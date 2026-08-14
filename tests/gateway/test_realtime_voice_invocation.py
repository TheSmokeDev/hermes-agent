"""Security contract for invocation-scoped realtime attachment capture."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import copy
import dataclasses
import gc
import hashlib
import json
import pickle
import threading
import weakref
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key
from gateway.session_state import SessionState
from hermes_cli.plugins import PluginContext, PluginManifest


def _source(*, user_id: str = "operator", chat_id: str = "voice-room") -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        user_id=user_id,
        chat_id=chat_id,
        chat_type="group",
        thread_id="thread-1",
        scope_id="guild-1",
    )


def _event(
    text: str = "/talk join", *, source: SessionSource | None = None
) -> MessageEvent:
    source = source or _source()
    return MessageEvent(
        text=text,
        source=source,
        user_id=source.user_id,
        message_id="message-1",
    )


def _plugin_context() -> PluginContext:
    return PluginContext(PluginManifest(name="talk"), object())


def _runner(source: SessionSource, *, execution_authority: bool = False):
    from gateway.run import GatewayRunner
    from gateway.realtime_voice_invocation import _register_gateway_runner
    from plugins.platforms.discord.adapter import DiscordAdapter

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="***")}
    )
    adapter = DiscordAdapter(runner.config.platforms[Platform.DISCORD])
    adapter.gateway_runner = runner
    runner.adapters = {Platform.DISCORD: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(), emit_collect=AsyncMock(return_value=[]), loaded_hooks=False
    )
    entry = SessionEntry(
        session_key=build_session_key(source),
        session_id="durable-session-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.DISCORD,
        chat_type=source.chat_type,
    )
    runner.session_store = MagicMock()
    runner.session_store._routing_generation = 7
    runner.session_store._generate_session_key.return_value = entry.session_key
    runner.session_store.get_exact_session_entry.return_value = entry
    runner.session_store.get_exact_session_entry_snapshot.return_value = (entry, 7)
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store._lock = threading.RLock()
    runner.session_store._entries = {entry.session_key: entry}
    runner.session_store._ensure_loaded_locked = lambda: None
    agent = SimpleNamespace(session_id=entry.session_id)
    state = SessionState()
    if execution_authority:
        state.turn.agent = agent
        state.turn.lease = object()
        state.turn.lease_token = object()
        state.turn.lease_generation = 11
        state.persistent.run_generation = 11
    runner._agent_cache_lock = threading.RLock()
    runner._agent_cache = OrderedDict()
    if execution_authority:
        runner._agent_cache[entry.session_key] = (
            agent,
            "signature",
            0,
            entry.session_id,
        )
    runner._session_states = {entry.session_key: state}
    runner._sessions = runner._session_states
    runner._running_agents = {}
    if execution_authority:
        # The legacy _sessions compatibility assignment rebuilds the turn view.
        state.turn.agent = agent
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    _register_gateway_runner(runner)
    return runner, entry


def _execution_runner(source: SessionSource):
    return _runner(source, execution_authority=True)


async def _invoke(runner, source, handler, *, authenticated=True, internal=False):
    from gateway.realtime_voice_invocation import _invoke_plugin_command_with_context

    return await _invoke_plugin_command_with_context(
        runner=runner,
        handler=handler,
        raw_args="join",
        source=source,
        routing_key=build_session_key(source),
        authenticated=authenticated,
        internal=internal,
    )


def test_plugin_context_capture_fails_outside_gateway_dispatch():
    from gateway.realtime_voice_invocation import RealtimeVoiceInvocationError

    with pytest.raises(
        RealtimeVoiceInvocationError, match="active opted-in plugin command"
    ):
        _plugin_context().capture_realtime_voice_attachment_factory()


@pytest.mark.asyncio
async def test_contextual_handler_receives_immutable_host_invocation_and_can_capture():
    from gateway.realtime_voice_invocation import (
        PluginCommandInvocation,
        RealtimeVoiceInvocationError,
        _validate_realtime_voice_attachment_factory,
    )

    source = _source()
    runner, entry = _runner(source)
    captured = []
    invocations = []

    def handler(raw_args, invocation):
        assert raw_args == "join"
        assert type(invocation) is PluginCommandInvocation
        with pytest.raises((AttributeError, TypeError)):
            invocation.extra = True
        invocations.append(invocation)
        captured.append(invocation.capture_realtime_voice_attachment_factory())
        return "joined"

    assert await _invoke(runner, source, handler) == "joined"
    binding = _validate_realtime_voice_attachment_factory(captured[0], runner)
    assert binding.durable_session_id == entry.session_id
    assert binding.principal_id == "operator"
    with pytest.raises(RealtimeVoiceInvocationError, match="active opted-in"):
        invocations[0].capture_realtime_voice_attachment_factory()


@pytest.mark.asyncio
async def test_contextual_invocation_mints_one_opaque_execution_attachment():
    from gateway.realtime_execution_attachment import RealtimeExecutionAttachment
    from gateway.realtime_voice_invocation import (
        RealtimeVoiceInvocationError,
        _is_host_realtime_execution_attachment,
    )

    source = _source()
    runner, _entry = _execution_runner(source)
    captured = []

    def handler(raw_args, invocation):
        assert raw_args == "join"
        attachment = invocation.capture_realtime_execution_attachment()
        assert attachment.closed is False
        captured.append(attachment)
        with pytest.raises(RealtimeVoiceInvocationError, match="exactly one"):
            invocation.capture_realtime_execution_attachment()
        return "joined"

    assert await _invoke(runner, source, handler) == "joined"
    attachment = captured[0]
    assert type(attachment) is RealtimeExecutionAttachment
    assert _is_host_realtime_execution_attachment(attachment)
    assert attachment.closed is False
    assert repr(attachment) == "<host realtime execution attachment>"
    assert [name for name in dir(attachment) if not name.startswith("_")] == [
        "close",
        "closed",
        "execute_tool_batch",
        "mint_tool_call_permit",
        "tool_definitions",
    ]
    for leaked_name in (
        "runner",
        "registry",
        "agent",
        "session_entry",
        "session_key",
        "principal",
        "route_pin",
        "credential",
        "handler",
        "approval_notifier",
    ):
        assert not hasattr(attachment, leaked_name)
    with pytest.raises(TypeError):
        vars(attachment)
    with pytest.raises(TypeError, match="cannot be serialized"):
        copy.copy(attachment)
    with pytest.raises(TypeError):
        pickle.dumps(attachment)
    with pytest.raises(TypeError):
        json.dumps(attachment)
    with pytest.raises(TypeError):
        RealtimeExecutionAttachment()

    attachment.close()
    assert attachment.closed is True
    attachment.close()
    assert not _is_host_realtime_execution_attachment(attachment)


@pytest.mark.asyncio
async def test_close_during_handler_revokes_provisional_execution_attachment():
    from gateway.realtime_voice_invocation import (
        RealtimeVoiceInvocationError,
        _is_host_realtime_execution_attachment,
    )

    source = _source()
    runner, _entry = _execution_runner(source)
    captured = []

    def handler(_args, invocation):
        attachment = invocation.capture_realtime_execution_attachment()
        captured.append(attachment)
        attachment.close()
        assert attachment.closed is True
        with pytest.raises(RealtimeVoiceInvocationError, match="exactly one"):
            invocation.capture_realtime_execution_attachment()

    await _invoke(runner, source, handler)
    assert captured[0].closed is True
    assert not _is_host_realtime_execution_attachment(captured[0])


@pytest.mark.asyncio
async def test_execution_capture_rejects_child_task_copied_context_and_thread():
    import contextvars

    from gateway.realtime_voice_invocation import RealtimeVoiceInvocationError

    source = _source()
    runner, _entry = _execution_runner(source)
    captured = []

    async def handler(_args, invocation):
        copied = contextvars.copy_context()

        async def child_capture():
            return copied.run(invocation.capture_realtime_execution_attachment)

        child = asyncio.create_task(child_capture())
        with pytest.raises(RealtimeVoiceInvocationError, match="exact gateway dispatch task"):
            await child
        with pytest.raises(RealtimeVoiceInvocationError, match="gateway dispatch thread"):
            await asyncio.to_thread(invocation.capture_realtime_execution_attachment)
        captured.append(invocation.capture_realtime_execution_attachment())

    await _invoke(runner, source, handler)
    assert captured[0].closed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "cancelled"])
async def test_failed_or_cancelled_handler_revokes_execution_attachment(failure):
    from gateway.realtime_voice_invocation import (
        _is_host_realtime_execution_attachment,
    )

    source = _source()
    runner, _entry = _execution_runner(source)
    captured = []

    async def handler(_args, invocation):
        captured.append(invocation.capture_realtime_execution_attachment())
        if failure == "cancelled":
            raise asyncio.CancelledError
        raise RuntimeError("failed")

    expected = asyncio.CancelledError if failure == "cancelled" else RuntimeError
    with pytest.raises(expected):
        await _invoke(runner, source, handler)
    assert captured[0].closed is True
    assert not _is_host_realtime_execution_attachment(captured[0])


@pytest.mark.asyncio
async def test_retained_or_lookalike_invocation_cannot_capture_for_another_command():
    from gateway.realtime_voice_invocation import (
        PluginCommandInvocation,
        RealtimeVoiceInvocationError,
    )

    source = _source()
    runner, _entry = _runner(source)
    retained = []
    await _invoke(runner, source, lambda _args, invocation: retained.append(invocation))

    with pytest.raises(RealtimeVoiceInvocationError, match="active opted-in"):
        retained[0].capture_realtime_execution_attachment()
    with pytest.raises(TypeError, match="host-minted"):
        PluginCommandInvocation()
    with pytest.raises(TypeError, match="cannot be serialized"):
        copy.copy(retained[0])

    async def wrong_command(_args, _invocation):
        with pytest.raises(RealtimeVoiceInvocationError, match="active opted-in"):
            retained[0].capture_realtime_execution_attachment()

    await _invoke(runner, source, wrong_command)


def test_plugin_load_and_noncontextual_handlers_have_no_execution_capture():
    context = _plugin_context()
    assert not hasattr(context, "capture_realtime_execution_attachment")
    calls = []

    def ordinary_handler(raw_args):
        calls.append(raw_args)

    ordinary_handler("join")
    assert calls == ["join"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "adapter",
        "routing_generation",
        "entry_recreation",
        "session_identity",
        "agent_lookalike",
        "run_generation",
        "lease",
        "lease_token",
    ],
)
async def test_execution_capture_rejects_route_drift_before_mint(mutation):
    from gateway.realtime_voice_invocation import (
        RealtimeVoiceInvocationError,
        _is_host_realtime_execution_attachment,
    )

    source = _source()
    runner, entry = _execution_runner(source)
    state = runner._sessions[entry.session_key]
    created = []

    def handler(_args, invocation):
        if mutation == "adapter":
            replacement = SimpleNamespace(gateway_runner=runner)
            runner.adapters[Platform.DISCORD] = replacement
        elif mutation == "routing_generation":
            runner.session_store.get_exact_session_entry_snapshot.return_value = (
                entry,
                8,
            )
        elif mutation == "entry_recreation":
            replacement = dataclasses.replace(entry)
            runner.session_store._entries[entry.session_key] = replacement
            runner.session_store.get_exact_session_entry_snapshot.return_value = (
                replacement,
                7,
            )
        elif mutation == "session_identity":
            entry.session_id = "recreated-session"
            state.turn.agent.session_id = entry.session_id
        elif mutation == "agent_lookalike":
            replacement = SimpleNamespace(session_id=entry.session_id)
            runner._agent_cache[entry.session_key] = (
                replacement,
                "signature",
                0,
                entry.session_id,
            )
            state.turn.agent = replacement
        elif mutation == "run_generation":
            state.persistent.run_generation += 1
            state.turn.lease_generation += 1
        elif mutation == "lease":
            state.turn.lease = object()
        else:
            state.turn.lease_token = object()
        created.append(invocation.capture_realtime_execution_attachment())

    with pytest.raises(RealtimeVoiceInvocationError, match="changed before capture"):
        await _invoke(runner, source, handler)
    assert created == []
    assert not any(_is_host_realtime_execution_attachment(item) for item in created)


@pytest.mark.asyncio
async def test_execution_attachment_retains_exact_private_pin_and_revalidates_live_route():
    from gateway.realtime_voice_invocation import (
        RealtimeVoiceInvocationError,
        _record_for_realtime_execution_attachment,
    )

    source = _source()
    runner, entry = _execution_runner(source)
    state = runner._sessions[entry.session_key]
    captured = []
    await _invoke(
        runner,
        source,
        lambda _args, invocation: captured.append(
            invocation.capture_realtime_execution_attachment()
        ),
    )

    record = _record_for_realtime_execution_attachment(captured[0], runner)
    assert record.runner_ref() is runner
    assert record.adapter_ref() is runner.adapters[Platform.DISCORD]
    assert record.entry_ref() is entry
    assert record.routing_generation == 7
    assert record.binding.principal_id == "operator"
    assert record.binding.profile is None
    assert record.binding.routing_key == entry.session_key
    assert record.binding.durable_session_id == entry.session_id
    assert record.route_pin.agent is state.turn.agent
    assert record.route_pin.session_entry is entry
    assert record.route_pin.generation == 11
    assert record.route_pin.active_session_lease is state.turn.lease
    assert record.route_pin.turn_lease_token is state.turn.lease_token

    state.turn.lease = object()
    with pytest.raises(RealtimeVoiceInvocationError, match="authority changed"):
        _record_for_realtime_execution_attachment(captured[0], runner)


@pytest.mark.asyncio
async def test_repeated_capture_is_idempotent_and_commits_one_factory():
    from gateway.realtime_voice_invocation import (
        _is_host_realtime_voice_attachment_factory,
        _validate_realtime_voice_attachment_factory,
    )

    source = _source()
    runner, _entry = _runner(source)
    captured = []

    def handler(_args, invocation):
        captured.append(invocation.capture_realtime_voice_attachment_factory())
        captured.append(invocation.capture_realtime_voice_attachment_factory())

    await _invoke(runner, source, handler)

    assert captured[0] is captured[1]
    assert _is_host_realtime_voice_attachment_factory(captured[0])
    assert _validate_realtime_voice_attachment_factory(
        captured[0], runner
    ).routing_key == (build_session_key(source))


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["group", "thread"])
async def test_production_discord_guild_sources_can_capture(chat_type):
    from gateway.realtime_voice_invocation import (
        _validate_realtime_voice_attachment_factory,
    )

    source = dataclasses.replace(_source(), chat_type=chat_type)
    runner, _entry = _runner(source)
    captured = []

    await _invoke(
        runner,
        source,
        lambda _args, invocation=None: captured.append(
            invocation.capture_realtime_voice_attachment_factory()
        ),
    )

    assert (
        _validate_realtime_voice_attachment_factory(captured[0], runner).chat_type
        == chat_type
    )


@pytest.mark.asyncio
async def test_stable_plugin_namespace_discord_adapter_can_capture():
    """Trust the exact runner-registered adapter, not its import alias identity."""

    from gateway.realtime_voice_invocation import (
        _validate_realtime_voice_attachment_factory,
    )

    source = _source()
    runner, _entry = _runner(source)

    # Production loads the bundled Discord plugin under the stable
    # ``hermes_plugins.discord_platform`` namespace.  Importing the same source
    # through ``plugins.platforms.discord`` creates a different class identity,
    # even though the runner owns this exact live adapter instance.
    class StableNamespaceDiscordAdapter:
        pass

    stable_namespace_adapter = StableNamespaceDiscordAdapter()
    stable_namespace_adapter.gateway_runner = runner
    runner.adapters[Platform.DISCORD] = stable_namespace_adapter
    captured = []

    await _invoke(
        runner,
        source,
        lambda _args, invocation: captured.append(
            invocation.capture_realtime_voice_attachment_factory()
        ),
    )

    binding = _validate_realtime_voice_attachment_factory(captured[0], runner)
    assert binding.routing_key == build_session_key(source)


@pytest.mark.asyncio
async def test_exact_host_factory_is_opaque_and_lookalikes_fail_closed():
    from gateway.realtime_voice_invocation import (
        _is_host_realtime_voice_attachment_factory,
        _validate_realtime_voice_attachment_factory,
    )

    source = _source()
    runner, _entry = _runner(source)
    captured = []
    await _invoke(
        runner,
        source,
        lambda _args, invocation: captured.append(
            invocation.capture_realtime_voice_attachment_factory()
        ),
    )
    factory = captured[0]

    assert _is_host_realtime_voice_attachment_factory(factory)
    binding = _validate_realtime_voice_attachment_factory(factory, runner)
    assert binding.routing_key == build_session_key(source)
    assert binding.durable_session_id == "durable-session-1"
    assert binding.principal_id == "operator"
    assert binding.chat_id == "voice-room"
    assert binding.thread_id == "thread-1"
    assert binding.scope_id == "guild-1"
    assert not hasattr(factory, "source")
    assert not hasattr(factory, "runner")

    with pytest.raises(TypeError, match="cannot be serialized"):
        copy.copy(factory)
    with pytest.raises(TypeError):
        type(factory)(binding)
    with pytest.raises((pickle.PicklingError, TypeError)):
        pickle.dumps(factory)
    assert not _is_host_realtime_voice_attachment_factory({
        "routing_key": "route-1",
        "durable_session_id": "session-1",
    })


@pytest.mark.asyncio
async def test_invocation_does_not_leak_to_background_tasks():
    from gateway.realtime_voice_invocation import RealtimeVoiceInvocationError

    runner, _entry = _runner(_source())

    async def handler(_args, invocation):
        task = asyncio.create_task(
            asyncio.to_thread(invocation.capture_realtime_voice_attachment_factory)
        )
        with pytest.raises(RealtimeVoiceInvocationError):
            await task

    await _invoke(runner, _source(), handler)


@pytest.mark.asyncio
async def test_concurrent_invocations_capture_only_their_own_source():
    from gateway.realtime_voice_invocation import (
        _validate_realtime_voice_attachment_factory,
    )

    ready = asyncio.Event()
    count = 0
    lock = asyncio.Lock()

    async def capture(user_id: str):
        nonlocal count
        source = _source(user_id=user_id, chat_id=f"room-{user_id}")
        runner, entry = _runner(source)
        entry.session_id = f"session-{user_id}"

        async def handler(_args, invocation):
            nonlocal count
            async with lock:
                count += 1
                if count == 2:
                    ready.set()
            await ready.wait()
            return invocation.capture_realtime_voice_attachment_factory()

        return runner, await _invoke(runner, source, handler)

    first, second = await asyncio.gather(capture("one"), capture("two"))
    first_binding = _validate_realtime_voice_attachment_factory(first[1], first[0])
    second_binding = _validate_realtime_voice_attachment_factory(second[1], second[0])
    assert first_binding.principal_id == "one"
    assert second_binding.principal_id == "two"


@pytest.mark.asyncio
async def test_gateway_plugin_dispatch_exposes_factory_and_preserves_ordinary_commands(
    monkeypatch,
):
    from gateway.realtime_voice_invocation import (
        _binding_for_realtime_voice_attachment_factory,
    )
    from hermes_cli import plugins as plugins_mod

    source = _source()
    runner, _entry = _runner(source)
    captured = []

    def handler(args: str, invocation=None):
        captured.append(invocation.capture_realtime_voice_attachment_factory())
        return f"joined {args}"

    monkeypatch.setattr(
        plugins_mod,
        "get_plugin_command_registration",
        lambda name: (
            {"handler": handler, "invocation_context": True} if name == "talk" else None
        ),
    )

    assert await runner._handle_message(_event(source=source)) == "joined join"
    binding = _binding_for_realtime_voice_attachment_factory(captured[0], runner)
    assert binding.routing_key == build_session_key(source)
    assert binding.durable_session_id == "durable-session-1"
    assert binding.principal_id == "operator"

    legacy_calls = []

    def legacy_handler(*args):
        from gateway.realtime_voice_invocation import RealtimeVoiceInvocationError

        legacy_calls.append(args)
        with pytest.raises(RealtimeVoiceInvocationError):
            _plugin_context().capture_realtime_voice_attachment_factory()
        return f"ordinary {args[0]}"

    monkeypatch.setattr(
        plugins_mod,
        "get_plugin_command_registration",
        lambda name: (
            {
                "handler": legacy_handler,
                "invocation_context": False,
            }
            if name == "plain"
            else None
        ),
    )
    assert (
        await runner._handle_message(_event("/plain works", source=source))
        == "ordinary works"
    )
    assert legacy_calls == [("works",)]


@pytest.mark.asyncio
async def test_non_discord_context_opt_in_keeps_one_argument_legacy_dispatch(
    monkeypatch,
):
    from hermes_cli import plugins as plugins_mod

    source = SessionSource(
        platform=Platform.SLACK,
        user_id="operator",
        chat_id="room",
        chat_type="channel",
        scope_id="workspace",
    )
    runner, _entry = _runner(source)
    calls = []

    def handler(*args):
        calls.append(args)
        return "legacy"

    monkeypatch.setattr(
        plugins_mod,
        "get_plugin_command_registration",
        lambda _name: {"handler": handler, "invocation_context": True},
    )

    assert await runner._handle_message(_event("/talk", source=source)) == "legacy"
    assert calls == [("",)]
    runner.session_store.get_exact_session_entry_snapshot.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [Platform.SLACK, Platform.TELEGRAM])
async def test_opted_handler_keeps_one_argument_gateway_compatibility(
    monkeypatch, platform
):
    from hermes_cli import plugins as plugins_mod

    source = SessionSource(
        platform=platform,
        user_id="operator",
        chat_id="room",
        chat_type="group",
        scope_id="scope",
    )
    runner, _entry = _runner(source)
    calls = []

    def handler(raw_args, invocation=None):
        calls.append((raw_args, invocation))
        return "ok"

    monkeypatch.setattr(
        plugins_mod,
        "get_plugin_command_registration",
        lambda _name: {"handler": handler, "invocation_context": True},
    )

    assert await runner._handle_message(_event("/talk join", source=source)) == "ok"
    assert calls == [("join", None)]


@pytest.mark.asyncio
async def test_capture_then_exception_revokes_factory():
    from gateway.realtime_voice_invocation import (
        _is_host_realtime_voice_attachment_factory,
    )

    runner, _entry = _runner(_source())
    captured = []

    def handler(_args, invocation):
        captured.append(invocation.capture_realtime_voice_attachment_factory())
        raise RuntimeError("handler failed")

    with pytest.raises(RuntimeError, match="handler failed"):
        await _invoke(runner, _source(), handler)
    assert not _is_host_realtime_voice_attachment_factory(captured[0])


@pytest.mark.asyncio
async def test_capture_then_cancellation_revokes_factory():
    from gateway.realtime_voice_invocation import (
        _is_host_realtime_voice_attachment_factory,
    )

    runner, _entry = _runner(_source())
    captured = []

    async def handler(_args, invocation):
        captured.append(invocation.capture_realtime_voice_attachment_factory())
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _invoke(runner, _source(), handler)
    assert not _is_host_realtime_voice_attachment_factory(captured[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,authenticated,internal",
    [
        (
            SessionSource(
                platform=Platform.SLACK,
                user_id="operator",
                chat_id="room",
                scope_id="guild",
            ),
            True,
            False,
        ),
        (_source(user_id=True), True, False),
        (_source(user_id=" operator"), True, False),
        (
            SessionSource(
                platform=Platform.DISCORD,
                user_id="operator",
                chat_id="room",
                scope_id=True,
            ),
            True,
            False,
        ),
        (dataclasses.replace(_source(), chat_type="channel"), True, False),
        (_source(), False, False),
        (_source(), True, True),
    ],
)
async def test_non_discord_unauthenticated_and_coercive_facts_fail_without_lookup(
    source, authenticated, internal
):
    from gateway.realtime_voice_invocation import RealtimeVoiceInvocationError

    runner, _entry = _runner(_source())
    with pytest.raises(RealtimeVoiceInvocationError):
        await _invoke(
            runner,
            source,
            lambda _args, _invocation: None,
            authenticated=authenticated,
            internal=internal,
        )
    runner.session_store.get_exact_session_entry_snapshot.assert_not_called()


@pytest.mark.asyncio
async def test_discord_bot_and_conflicting_guild_alias_fail_closed_before_lookup():
    from gateway.realtime_voice_invocation import RealtimeVoiceInvocationError

    runner, _entry = _runner(_source())
    bot_source = _source()
    bot_source.is_bot = True
    with pytest.raises(RealtimeVoiceInvocationError):
        await _invoke(runner, bot_source, lambda _args, _invocation: None)

    conflicting = _source()
    conflicting.guild_id = "other-guild"
    with pytest.raises(RealtimeVoiceInvocationError):
        await _invoke(runner, conflicting, lambda _args, _invocation: None)
    runner.session_store.get_exact_session_entry_snapshot.assert_not_called()


@pytest.mark.asyncio
async def test_factory_validation_pins_runner_entry_identity_and_generation():
    from gateway.realtime_voice_invocation import (
        RealtimeVoiceInvocationError,
        _validate_realtime_voice_attachment_factory,
    )

    source = _source()
    runner, entry = _runner(source)
    captured = []
    await _invoke(
        runner,
        source,
        lambda _a, inv: captured.append(
            inv.capture_realtime_voice_attachment_factory()
        ),
    )
    factory = captured[0]

    other_runner, _ = _runner(source)
    with pytest.raises(RealtimeVoiceInvocationError):
        _validate_realtime_voice_attachment_factory(factory, other_runner)

    equivalent = dataclasses.replace(entry)
    runner.session_store.get_exact_session_entry_snapshot.return_value = (equivalent, 7)
    with pytest.raises(RealtimeVoiceInvocationError):
        _validate_realtime_voice_attachment_factory(factory, runner)

    runner.session_store.get_exact_session_entry_snapshot.return_value = (entry, 8)
    runner.session_store._routing_generation += 1
    with pytest.raises(RealtimeVoiceInvocationError):
        _validate_realtime_voice_attachment_factory(factory, runner)


@pytest.mark.asyncio
async def test_missing_existing_entry_fails_without_creating_session():
    from gateway.realtime_voice_invocation import RealtimeVoiceInvocationError

    source = _source()
    runner, _entry = _runner(source)
    runner.session_store.get_exact_session_entry_snapshot.return_value = (None, 7)

    with pytest.raises(RealtimeVoiceInvocationError, match="existing durable session"):
        await _invoke(runner, source, lambda _args, _invocation: None)
    runner.session_store.get_or_create_session.assert_not_called()


@pytest.mark.asyncio
async def test_unregistered_runner_cannot_mint_even_with_valid_public_facts():
    from gateway.realtime_voice_invocation import RealtimeVoiceInvocationError
    from gateway.run import GatewayRunner

    source = _source()
    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    with pytest.raises(RealtimeVoiceInvocationError, match="host-owned dispatch state"):
        await _invoke(runner, source, lambda _args, _invocation: None)
    runner.session_store.get_exact_session_entry_snapshot.assert_not_called()


def test_session_store_exact_lookup_is_noncreating_and_identity_preserving():
    from gateway.session import SessionStore

    source = _source()
    entry = SessionEntry(
        session_key=build_session_key(source),
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    store = object.__new__(SessionStore)
    store._entries = {entry.session_key: entry}
    store._loaded = True
    store._lock = threading.Lock()

    assert store.get_exact_session_entry(entry.session_key) is entry
    assert store.get_exact_session_entry("missing") is None
    assert store.get_exact_session_entry_snapshot(entry.session_key) == (entry, 0)


def test_route_snapshots_are_isolated_and_only_structural_changes_invalidate():
    from gateway.session import SessionStore

    source_a = _source(chat_id="room-a")
    source_b = _source(chat_id="room-b")
    route_a = build_session_key(source_a)
    route_b = build_session_key(source_b)
    now = datetime.now()
    entry_a = SessionEntry(route_a, "sid-a", now, now, origin=source_a)
    entry_b = SessionEntry(route_b, "sid-b", now, now, origin=source_b)
    store = object.__new__(SessionStore)
    store._entries = {route_a: entry_a, route_b: entry_b}
    store._loaded = True
    store._lock = threading.Lock()
    store._db = None
    store._save = lambda: None
    store._save_entry = lambda *_args, **_kwargs: None

    captured_a = store.get_exact_session_entry_snapshot(route_a)
    store.update_session(route_a, last_prompt_tokens=3)
    assert store.get_exact_session_entry_snapshot(route_a) == captured_a

    store.update_session(route_b, last_prompt_tokens=4)
    store.switch_session(route_b, "sid-b2")
    assert store.get_exact_session_entry_snapshot(route_a) == captured_a

    before_reset_b = store.get_exact_session_entry_snapshot(route_b)
    store.reset_session(route_b)
    after_reset_b = store.get_exact_session_entry_snapshot(route_b)
    assert after_reset_b[0] is not before_reset_b[0]
    assert after_reset_b[1] != before_reset_b[1]

    before_compression_b = after_reset_b
    assert (
        store.advance_compression_session(
            route_b,
            before_compression_b[0].session_id,
            "sid-b-compressed",
        )
        is before_compression_b[0]
    )
    after_compression_b = store.get_exact_session_entry_snapshot(route_b)
    assert after_compression_b[0] is before_compression_b[0]
    assert after_compression_b[1] != before_compression_b[1]

    store.switch_session(route_a, "sid-a2")
    current_a = store.get_exact_session_entry_snapshot(route_a)
    assert current_a[0] is not captured_a[0]
    assert current_a[1] != captured_a[1]


def test_route_generation_is_discarded_and_recreate_gets_fresh_live_version():
    from datetime import timedelta
    from gateway.session import SessionStore

    source = _source(chat_id="recreated-room")
    route = build_session_key(source)
    old = datetime.now() - timedelta(days=10)
    entry = SessionEntry(route, "sid-old", old, old, origin=source)
    store = object.__new__(SessionStore)
    store._entries = {route: entry}
    store._route_structural_generations = {route: 1}
    store._loaded = True
    store._lock = threading.Lock()
    store._db = None
    store._save = lambda: None
    store._save_entries = lambda: None
    store._has_active_processes_fn = None
    store._generate_session_key = lambda _source: route
    store._query_recoverable_session = lambda **_kwargs: None

    before = store.get_exact_session_entry_snapshot(route)
    assert store.prune_old_entries(1) == 1
    removed = store.get_exact_session_entry_snapshot(route)
    assert removed[0] is None
    assert route not in store._route_structural_generations

    recreated = store._get_or_create_session_impl(source)
    after = store.get_exact_session_entry_snapshot(route)
    assert after[0] is recreated
    assert after[0] is not before[0]
    assert after[1] == before[1]


def test_pruned_route_generation_bookkeeping_is_bounded_to_live_entries():
    from datetime import timedelta
    from gateway.session import SessionStore

    old = datetime.now() - timedelta(days=10)
    store = object.__new__(SessionStore)
    store._entries = {
        f"route-{index}": SessionEntry(f"route-{index}", f"sid-{index}", old, old)
        for index in range(2000)
    }
    store._route_structural_generations = {key: 1 for key in store._entries}
    store._loaded = True
    store._lock = threading.Lock()
    store._db = None
    store._save = lambda: None
    store._has_active_processes_fn = None

    assert store.prune_old_entries(1) == 2000
    assert store._entries == {}
    assert store._route_structural_generations == {}


@pytest.mark.asyncio
async def test_compression_retry_reroute_invalidates_only_matching_route_factory():
    from hermes_state import CompressionSessionClosedError
    from gateway.realtime_voice_invocation import (
        RealtimeVoiceInvocationError,
        _validate_realtime_voice_attachment_factory,
    )
    from gateway.session import SessionStore

    source = _source(chat_id="compressed-room")
    unrelated_source = _source(chat_id="unrelated-room")
    route = build_session_key(source)
    unrelated_route = build_session_key(unrelated_source)
    now = datetime.now()
    entry = SessionEntry(route, "parent", now, now, origin=source)
    unrelated = SessionEntry(
        unrelated_route, "other", now, now, origin=unrelated_source
    )

    class FakeDb:
        def find_live_compression_child(self, session_id):
            assert session_id == "parent"
            return {"id": "child"}

    store = object.__new__(SessionStore)
    store._db = FakeDb()
    store._entries = {route: entry, unrelated_route: unrelated}
    store._loaded = True
    store._lock = threading.RLock()
    store._save = lambda: None
    store._transcript_retry_lock = threading.Lock()
    store._dirty_transcripts = {}
    store._transcript_append_failures = {}
    store._transcript_reroutes = {}
    store._fts_rebuild_attempted = True

    def append(session_id, _message):
        if session_id == "parent":
            raise CompressionSessionClosedError("parent")

    store._append_transcript_message = append
    runner, _ = _runner(source)
    runner.session_store = store
    captured = []
    await _invoke(
        runner,
        source,
        lambda _args, invocation: captured.append(
            invocation.capture_realtime_voice_attachment_factory()
        ),
    )
    before = store.get_exact_session_entry_snapshot(route)
    unrelated_before = store.get_exact_session_entry_snapshot(unrelated_route)

    store.append_to_transcript("parent", {"role": "assistant", "content": "reroute"})

    after = store.get_exact_session_entry_snapshot(route)
    assert entry.session_id == "child"
    assert after[1] != before[1]
    assert store.get_exact_session_entry_snapshot(unrelated_route) == unrelated_before
    with pytest.raises(RealtimeVoiceInvocationError, match="stale"):
        _validate_realtime_voice_attachment_factory(captured[0], runner)


def test_compression_retry_without_matching_route_does_not_advance_generations():
    from hermes_state import CompressionSessionClosedError
    from gateway.session import SessionStore

    source = _source(chat_id="unrelated-room")
    route = build_session_key(source)
    now = datetime.now()
    entry = SessionEntry(route, "other", now, now, origin=source)

    class FakeDb:
        def find_live_compression_child(self, session_id):
            assert session_id == "parent"
            return {"id": "child"}

    store = object.__new__(SessionStore)
    store._db = FakeDb()
    store._entries = {route: entry}
    store._loaded = True
    store._lock = threading.RLock()
    store._save = lambda: None
    store._transcript_retry_lock = threading.Lock()
    store._dirty_transcripts = {}
    store._transcript_append_failures = {}
    store._transcript_reroutes = {}
    store._fts_rebuild_attempted = True

    def append(session_id, _message):
        if session_id == "parent":
            raise CompressionSessionClosedError("parent")

    store._append_transcript_message = append
    before = store.get_exact_session_entry_snapshot(route)

    store.append_to_transcript("parent", {"role": "assistant", "content": "reroute"})

    assert store.get_exact_session_entry_snapshot(route) == before


async def _committed_execution_attachment(runner, source):
    captured = []
    await _invoke(
        runner,
        source,
        lambda _args, invocation: captured.append(
            invocation.capture_realtime_execution_attachment()
        ),
    )
    return captured[0]


@pytest.mark.asyncio
async def test_execution_attachment_projects_exact_live_curated_tool_surface():
    source = _source()
    runner, _entry = _execution_runner(source)
    agent = runner._session_states[build_session_key(source)].turn.agent
    permitted = {
        "type": "function",
        "function": {
            "name": "permitted_tool",
            "description": "Already authorized by the live agent.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }
    hidden = {
        "type": "function",
        "function": {
            "name": "hidden_tool",
            "description": "Denied by canonical host curation.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    agent.tools = [permitted]
    agent.valid_tool_names = {"permitted_tool"}
    agent._denied_test_tool = hidden

    attachment = await _committed_execution_attachment(runner, source)

    assert attachment.tool_definitions() == [permitted["function"]]
    assert "hidden_tool" not in repr(attachment.tool_definitions())

    first = attachment.tool_definitions()
    second = attachment.tool_definitions()
    assert first == second
    assert first is not second
    assert first[0] is not second[0]
    assert first[0]["parameters"] is not second[0]["parameters"]
    first[0]["parameters"]["properties"]["query"]["type"] = "integer"
    assert second[0]["parameters"]["properties"]["query"]["type"] == "string"
    json.dumps(second, allow_nan=False)


@pytest.mark.asyncio
async def test_execution_attachment_projects_canonical_tools_without_defaults():
    from model_tools import get_tool_definitions

    source = _source()
    runner, _entry = _execution_runner(source)
    agent = runner._session_states[build_session_key(source)].turn.agent
    canonical_tools = copy.deepcopy(get_tool_definitions(quiet_mode=True))
    by_name = {tool["function"]["name"]: tool for tool in canonical_tools}
    representative_names = {"computer_use", "read_file", "search_files", "terminal"}
    assert representative_names <= by_name.keys()

    secret_default = "sk-canonical-default-must-never-escape"
    opaque_default = [{"nested": [None, True, 7, 1.5]}]
    probe_properties = by_name["read_file"]["function"]["parameters"][
        "properties"
    ]
    probe_properties["default_probe"] = {
        "type": "object",
        "properties": {
            "secret": {"type": "string", "default": secret_default},
            "opaque": {"type": "string", "default": opaque_default},
        },
    }
    agent.tools = canonical_tools
    agent.valid_tool_names = set(by_name)
    attachment = await _committed_execution_attachment(runner, source)

    first = attachment.tool_definitions()
    second = attachment.tool_definitions()
    projected_names = {tool["name"] for tool in first}
    assert representative_names <= projected_names

    def assert_no_defaults(value):
        if type(value) is dict:
            assert "default" not in value
            for item in value.values():
                assert_no_defaults(item)
        elif type(value) is list:
            for item in value:
                assert_no_defaults(item)

    assert_no_defaults(first)
    assert secret_default not in repr(first)
    json.dumps(first, allow_nan=False)
    assert first == second
    assert first is not second
    first[0]["parameters"]["properties"]["detached"] = {"type": "null"}
    assert "detached" not in second[0]["parameters"]["properties"]
    assert "detached" not in canonical_tools[0]["function"]["parameters"][
        "properties"
    ]
    assert (
        probe_properties["default_probe"]["properties"]["secret"]["default"]
        == secret_default
    )
    assert (
        probe_properties["default_probe"]["properties"]["opaque"]["default"]
        is opaque_default
    )


def _tool_with_default(default):
    return {
        "type": "function",
        "function": {
            "name": "bounded_default_tool",
            "description": "Default resource-bound probe.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string", "default": default}},
            },
        },
    }


def test_stripped_million_element_default_is_rejected_by_node_bound():
    from gateway.realtime_voice_invocation import (
        RealtimeVoiceInvocationError,
        _project_realtime_tool_definitions,
    )

    default = [None] * 1_000_000
    tool = _tool_with_default(default)

    with pytest.raises(RealtimeVoiceInvocationError, match="tool schema"):
        _project_realtime_tool_definitions([tool], {"bounded_default_tool"})
    assert tool["function"]["parameters"]["properties"]["value"]["default"] is default


def test_stripped_thousand_level_default_is_rejected_by_depth_bound():
    from gateway.realtime_voice_invocation import (
        RealtimeVoiceInvocationError,
        _project_realtime_tool_definitions,
    )

    default = None
    for _ in range(1_000):
        default = [default]
    tool = _tool_with_default(default)

    with pytest.raises(RealtimeVoiceInvocationError, match="tool schema"):
        _project_realtime_tool_definitions([tool], {"bounded_default_tool"})


@pytest.mark.parametrize(
    "default",
    [
        "x" * 262_145,
        {str(index): None for index in range(4_097)},
        [None] * 4_097,
    ],
    ids=["string-bytes", "dict-nodes", "list-nodes"],
)
def test_stripped_default_is_rejected_by_aggregate_resource_bounds(default):
    from gateway.realtime_voice_invocation import (
        RealtimeVoiceInvocationError,
        _project_realtime_tool_definitions,
    )

    tool = _tool_with_default(default)
    with pytest.raises(RealtimeVoiceInvocationError, match="tool schema"):
        _project_realtime_tool_definitions([tool], {"bounded_default_tool"})


def test_stripped_defaults_share_discarded_input_byte_budget():
    from gateway.realtime_voice_invocation import (
        RealtimeVoiceInvocationError,
        _project_realtime_tool_definitions,
    )

    tool = _tool_with_default("x" * 100_000)
    properties = tool["function"]["parameters"]["properties"]
    properties["second"] = {"type": "string", "default": "y" * 100_000}
    properties["third"] = {"type": "string", "default": "z" * 100_000}

    with pytest.raises(RealtimeVoiceInvocationError, match="tool schema"):
        _project_realtime_tool_definitions([tool], {"bounded_default_tool"})


@pytest.mark.parametrize(
    "case", ["object", "nan", "dict-subclass", "non-string-key"]
)
def test_stripped_default_still_requires_exact_json_values(case):
    from gateway.realtime_voice_invocation import (
        RealtimeVoiceInvocationError,
        _project_realtime_tool_definitions,
    )

    class DictLookalike(dict):
        pass

    default = {
        "object": object(),
        "nan": float("nan"),
        "dict-subclass": DictLookalike({"key": None}),
        "non-string-key": {1: None},
    }[case]
    tool = _tool_with_default(default)
    with pytest.raises(RealtimeVoiceInvocationError, match="tool schema"):
        _project_realtime_tool_definitions([tool], {"bounded_default_tool"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "wrapper_subclass",
        "function_subclass",
        "name_subclass",
        "bad_name",
        "description_subclass",
        "description_oversize",
        "parameters_subclass",
        "non_json",
        "too_deep",
        "duplicate",
        "too_many",
        "too_large",
    ],
)
async def test_execution_attachment_rejects_unsafe_tool_schema_matrix(case):
    from gateway.realtime_voice_invocation import RealtimeVoiceInvocationError

    secret_value = "sk-never-leak-this-value"

    class DictLookalike(dict):
        pass

    class StringLookalike(str):
        pass

    def schema(name="safe_tool"):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": "Safe description.",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    source = _source()
    runner, _entry = _execution_runner(source)
    agent = runner._session_states[build_session_key(source)].turn.agent
    tools = [schema()]
    if case == "wrapper_subclass":
        tools[0] = DictLookalike(tools[0])
    elif case == "function_subclass":
        tools[0]["function"] = DictLookalike(tools[0]["function"])
    elif case == "name_subclass":
        tools[0]["function"]["name"] = StringLookalike("safe_tool")
    elif case == "bad_name":
        tools[0]["function"]["name"] = " unsafe tool "
    elif case == "description_subclass":
        tools[0]["function"]["description"] = StringLookalike("unsafe")
    elif case == "description_oversize":
        tools[0]["function"]["description"] = "d" * 16_385
    elif case == "parameters_subclass":
        tools[0]["function"]["parameters"] = DictLookalike(
            tools[0]["function"]["parameters"]
        )
    elif case == "non_json":
        tools[0]["function"]["parameters"]["properties"]["x"] = {
            "enum": (secret_value, object())
        }
    elif case == "too_deep":
        node = tools[0]["function"]["parameters"]
        for _ in range(20):
            child = {}
            node["nested"] = child
            node = child
    elif case == "duplicate":
        tools.append(schema())
    elif case == "too_many":
        tools = [schema(f"tool_{index}") for index in range(129)]
    elif case == "too_large":
        tools[0]["function"]["parameters"]["description"] = "x" * 300_000
    agent.tools = tools
    agent.valid_tool_names = {
        tool["function"]["name"] for tool in tools if type(tool) is dict
    }
    attachment = await _committed_execution_attachment(runner, source)

    with pytest.raises(RealtimeVoiceInvocationError, match="tool schema") as exc_info:
        attachment.tool_definitions()
    assert secret_value not in str(exc_info.value)


@pytest.mark.asyncio
async def test_tool_definitions_require_commit_allow_child_threads_and_fail_after_close():
    from gateway.realtime_voice_invocation import RealtimeVoiceInvocationError

    source = _source()
    runner, _entry = _execution_runner(source)
    agent = runner._session_states[build_session_key(source)].turn.agent
    agent.tools = [
        {
            "type": "function",
            "function": {
                "name": "thread_safe_tool",
                "description": "Thread-safe provider projection.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    agent.valid_tool_names = {"thread_safe_tool"}
    captured = []

    def handler(_args, invocation):
        attachment = invocation.capture_realtime_execution_attachment()
        captured.append(attachment)
        with pytest.raises(RealtimeVoiceInvocationError, match="authority changed"):
            attachment.tool_definitions()

    await _invoke(runner, source, handler)
    attachment = captured[0]
    results = await asyncio.gather(
        *(asyncio.to_thread(attachment.tool_definitions) for _ in range(8))
    )
    assert all(result == results[0] for result in results)
    assert len({id(result) for result in results}) == len(results)
    assert len({id(result[0]) for result in results}) == len(results)

    attachment.close()
    with pytest.raises(RealtimeVoiceInvocationError, match="authority changed"):
        attachment.tool_definitions()


@pytest.mark.asyncio
async def test_tool_definitions_rotation_wins_before_projection_can_return(
    monkeypatch,
):
    import gateway.realtime_voice_invocation as invocation_module

    source = _source()
    runner, _entry = _execution_runner(source)
    state = runner._session_states[build_session_key(source)]
    agent = state.turn.agent
    agent.tools = [
        {
            "type": "function",
            "function": {
                "name": "stale_tool",
                "description": "Must not escape after rotation wins.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    agent.valid_tool_names = {"stale_tool"}
    attachment = await _committed_execution_attachment(runner, source)
    projection_entered = threading.Event()
    permit_projection = threading.Event()
    original_project = invocation_module._project_realtime_tool_definitions

    def blocked_project(tools, valid_names):
        projection_entered.set()
        assert permit_projection.wait(timeout=5)
        return original_project(tools, valid_names)

    monkeypatch.setattr(
        invocation_module, "_project_realtime_tool_definitions", blocked_project
    )
    pending = asyncio.create_task(asyncio.to_thread(attachment.tool_definitions))
    assert await asyncio.to_thread(projection_entered.wait, 5)
    with state.turn_authority_lock:
        state.persistent.run_generation += 1
    permit_projection.set()

    with pytest.raises(
        invocation_module.RealtimeVoiceInvocationError, match="authority changed"
    ):
        await pending


@pytest.mark.asyncio
async def test_execution_attachment_mints_opaque_exact_tool_call_permit():
    from gateway.realtime_execution_attachment import RealtimeToolCallPermit
    from gateway.realtime_voice_invocation import (
        _record_for_realtime_tool_call_permit,
    )

    source = _source()
    runner, _entry = _execution_runner(source)
    state = runner._session_states[build_session_key(source)]
    agent = state.turn.agent
    agent.tools = [
        {
            "type": "function",
            "function": {
                "name": "permitted_tool",
                "description": "Canonical permit probe.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    agent.valid_tool_names = {"permitted_tool"}
    attachment = await _committed_execution_attachment(runner, source)
    arguments = {"z": [1, True, None], "a": {"value": "safe"}}

    permit = attachment.mint_tool_call_permit(
        response_id="response-1",
        item_id="item-2",
        call_id="call-3",
        batch_id="batch-4",
        tool_name="permitted_tool",
        arguments=arguments,
    )
    arguments["z"][0] = 999
    arguments["a"]["value"] = "mutated"

    assert type(permit) is RealtimeToolCallPermit
    assert repr(permit) == "<host realtime tool call permit>"
    assert not hasattr(permit, "execute")
    assert not hasattr(permit, "response_id")
    assert not hasattr(permit, "arguments")
    with pytest.raises((AttributeError, TypeError)):
        permit.extra = True
    with pytest.raises(TypeError):
        pickle.dumps(permit)

    record = _record_for_realtime_tool_call_permit(attachment, permit)
    canonical = b'{"a":{"value":"safe"},"z":[1,true,null]}'
    assert record.attachment_ref() is attachment
    assert record.attachment_record.route_pin is record.route_pin
    assert record.route_pin.agent is agent
    assert record.tool_surface is agent.tools
    assert record.valid_tool_names is agent.valid_tool_names
    assert record.response_id == "response-1"
    assert record.item_id == "item-2"
    assert record.call_id == "call-3"
    assert record.batch_id == "batch-4"
    assert record.tool_name == "permitted_tool"
    assert record.arguments_bytes == canonical
    assert record.arguments_digest == hashlib.sha256(canonical).digest()
    assert record.consumed is False
    public = repr(permit)
    for secret in ("response-1", "item-2", "call-3", "batch-4", "safe"):
        assert secret not in public


def _install_permit_tool(runner, source, name="permitted_tool"):
    agent = runner._session_states[build_session_key(source)].turn.agent
    agent.tools = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": "Permit matrix tool.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    agent.valid_tool_names = {name}
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "id-subclass",
        "id-empty",
        "id-whitespace",
        "id-control",
        "id-surrogate",
        "id-bytes",
        "arguments-subclass",
        "nested-dict-subclass",
        "nested-list-subclass",
        "non-string-key",
        "nan",
        "infinity",
        "argument-surrogate",
        "argument-depth",
        "argument-nodes",
        "argument-keys",
        "argument-bytes",
        "name-subclass",
        "unknown-name",
    ],
)
async def test_tool_call_permit_rejects_hostile_ids_arguments_and_names(case):
    from gateway.realtime_voice_invocation import RealtimeVoiceInvocationError

    class StringLookalike(str):
        pass

    class DictLookalike(dict):
        pass

    class ListLookalike(list):
        pass

    source = _source()
    runner, _entry = _execution_runner(source)
    _install_permit_tool(runner, source)
    attachment = await _committed_execution_attachment(runner, source)
    values = {
        "response_id": "response-safe",
        "item_id": "item-safe",
        "call_id": "call-safe",
        "batch_id": "batch-safe",
        "tool_name": "permitted_tool",
        "arguments": {"safe": True},
    }
    if case == "id-subclass":
        values["call_id"] = StringLookalike("secret-subclass")
    elif case == "id-empty":
        values["item_id"] = ""
    elif case == "id-whitespace":
        values["response_id"] = " secret-whitespace "
    elif case == "id-control":
        values["batch_id"] = "secret\u0000control"
    elif case == "id-surrogate":
        values["call_id"] = "secret\ud800surrogate"
    elif case == "id-bytes":
        values["call_id"] = "x" * 513
    elif case == "arguments-subclass":
        values["arguments"] = DictLookalike({"secret": True})
    elif case == "nested-dict-subclass":
        values["arguments"] = {"value": DictLookalike({"secret": True})}
    elif case == "nested-list-subclass":
        values["arguments"] = {"value": ListLookalike(["secret"])}
    elif case == "non-string-key":
        values["arguments"] = {1: "secret"}
    elif case == "nan":
        values["arguments"] = {"value": float("nan")}
    elif case == "infinity":
        values["arguments"] = {"value": float("inf")}
    elif case == "argument-surrogate":
        values["arguments"] = {"value": "secret\ud800surrogate"}
    elif case == "argument-depth":
        nested = None
        for _ in range(18):
            nested = [nested]
        values["arguments"] = {"value": nested}
    elif case == "argument-nodes":
        values["arguments"] = {"value": [None] * 4_097}
    elif case == "argument-keys":
        values["arguments"] = {str(index): None for index in range(4_097)}
    elif case == "argument-bytes":
        values["arguments"] = {"value": "x" * 262_145}
    elif case == "name-subclass":
        values["tool_name"] = StringLookalike("permitted_tool")
    elif case == "unknown-name":
        values["tool_name"] = "secret_unknown_tool"

    with pytest.raises(RealtimeVoiceInvocationError) as exc_info:
        attachment.mint_tool_call_permit(**values)
    assert "secret" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_tool_call_permit_duplicate_identity_positions_and_attachment_scope():
    from gateway.realtime_execution_attachment import RealtimeToolCallPermit
    from gateway.realtime_voice_invocation import (
        RealtimeVoiceInvocationError,
        _record_for_realtime_tool_call_permit,
    )

    with pytest.raises(TypeError):
        RealtimeToolCallPermit()
    source = _source()
    runner, _entry = _execution_runner(source)
    _install_permit_tool(runner, source)
    first_attachment = await _committed_execution_attachment(runner, source)
    second_attachment = await _committed_execution_attachment(runner, source)
    common = dict(
        response_id="same",
        item_id="same",
        call_id="same",
        batch_id="same",
        tool_name="permitted_tool",
        arguments={},
    )
    permit = first_attachment.mint_tool_call_permit(**common)
    record = _record_for_realtime_tool_call_permit(first_attachment, permit)
    assert (record.response_id, record.item_id, record.call_id, record.batch_id) == (
        "same",
        "same",
        "same",
        "same",
    )
    with pytest.raises(RealtimeVoiceInvocationError, match="already admitted"):
        first_attachment.mint_tool_call_permit(**common)
    for field in ("response_id", "item_id", "call_id", "batch_id"):
        distinct = dict(common)
        distinct[field] = f"different-{field}"
        first_attachment.mint_tool_call_permit(**distinct)
    with pytest.raises(RealtimeVoiceInvocationError, match="unavailable"):
        _record_for_realtime_tool_call_permit(second_attachment, permit)


@pytest.mark.asyncio
async def test_tool_call_permit_duplicate_close_and_rotation_barriers(monkeypatch):
    import gateway.realtime_voice_invocation as invocation_module

    source = _source()
    runner, _entry = _execution_runner(source)
    _install_permit_tool(runner, source)
    attachment = await _committed_execution_attachment(runner, source)
    common = dict(
        response_id="response-race",
        item_id="item-race",
        call_id="call-race",
        batch_id="batch-race",
        tool_name="permitted_tool",
        arguments={},
    )
    original = invocation_module._live_tool_surface_for_permit
    first_calls = threading.Barrier(2)
    thread_counts = {}
    counts_lock = threading.Lock()

    def duplicate_barrier(record, tool_name):
        ident = threading.get_ident()
        with counts_lock:
            count = thread_counts.get(ident, 0)
            thread_counts[ident] = count + 1
        if count == 0:
            first_calls.wait(timeout=5)
        return original(record, tool_name)

    monkeypatch.setattr(
        invocation_module, "_live_tool_surface_for_permit", duplicate_barrier
    )

    def mint_duplicate():
        try:
            return attachment.mint_tool_call_permit(**common)
        except Exception as exc:  # exact outcome asserted below
            return exc

    outcomes = await asyncio.gather(
        asyncio.to_thread(mint_duplicate), asyncio.to_thread(mint_duplicate)
    )
    assert sum(type(item).__name__ == "RealtimeToolCallPermit" for item in outcomes) == 1
    failures = [item for item in outcomes if isinstance(item, Exception)]
    assert len(failures) == 1
    assert "already admitted" in str(failures[0])

    monkeypatch.setattr(invocation_module, "_live_tool_surface_for_permit", original)
    close_attachment = await _committed_execution_attachment(runner, source)
    entered = threading.Event()
    release = threading.Event()

    def close_barrier(record, tool_name):
        entered.set()
        assert release.wait(timeout=5)
        return original(record, tool_name)

    monkeypatch.setattr(
        invocation_module, "_live_tool_surface_for_permit", close_barrier
    )
    pending_close = asyncio.create_task(
        asyncio.to_thread(
            close_attachment.mint_tool_call_permit,
            response_id="response-close",
            item_id="item-close",
            call_id="call-close",
            batch_id="batch-close",
            tool_name="permitted_tool",
            arguments={},
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    close_attachment.close()
    release.set()
    with pytest.raises(invocation_module.RealtimeVoiceInvocationError):
        await pending_close

    monkeypatch.setattr(invocation_module, "_live_tool_surface_for_permit", original)
    rotation_attachment = await _committed_execution_attachment(runner, source)
    entered = threading.Event()
    release = threading.Event()

    def rotation_barrier(record, tool_name):
        entered.set()
        assert release.wait(timeout=5)
        return original(record, tool_name)

    monkeypatch.setattr(
        invocation_module, "_live_tool_surface_for_permit", rotation_barrier
    )
    pending_rotation = asyncio.create_task(
        asyncio.to_thread(
            rotation_attachment.mint_tool_call_permit,
            response_id="response-rotation",
            item_id="item-rotation",
            call_id="call-rotation",
            batch_id="batch-rotation",
            tool_name="permitted_tool",
            arguments={},
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    state = runner._session_states[build_session_key(source)]
    with state.turn_authority_lock:
        state.persistent.run_generation += 1
    release.set()
    with pytest.raises(
        invocation_module.RealtimeVoiceInvocationError, match="authority changed"
    ):
        await pending_rotation


@pytest.mark.asyncio
async def test_tool_call_permit_capacity_close_and_gc_cleanup():
    import gateway.realtime_voice_invocation as invocation_module

    source = _source()
    runner, _entry = _execution_runner(source)
    _install_permit_tool(runner, source)
    attachment = await _committed_execution_attachment(runner, source)
    permits = [
        attachment.mint_tool_call_permit(
            response_id=f"response-{index}",
            item_id=f"item-{index}",
            call_id=f"call-{index}",
            batch_id=f"batch-{index}",
            tool_name="permitted_tool",
            arguments={},
        )
        for index in range(invocation_module._MAX_OUTSTANDING_TOOL_CALL_PERMITS)
    ]
    with pytest.raises(
        invocation_module.RealtimeVoiceInvocationError, match="capacity"
    ):
        attachment.mint_tool_call_permit(
            response_id="response-overflow",
            item_id="item-overflow",
            call_id="call-overflow",
            batch_id="batch-overflow",
            tool_name="permitted_tool",
            arguments={},
        )
    retired = permits.pop()
    retired_ref = weakref.ref(retired)
    del retired
    gc.collect()
    assert retired_ref() is None
    replacement = attachment.mint_tool_call_permit(
        response_id="response-replacement",
        item_id="item-replacement",
        call_id="call-replacement",
        batch_id="batch-replacement",
        tool_name="permitted_tool",
        arguments={},
    )
    permits.append(replacement)
    attachment.close()
    assert not any(
        permit in invocation_module._tool_call_permit_records for permit in permits
    )

    gc_attachment = await _committed_execution_attachment(runner, source)
    retained_permit = gc_attachment.mint_tool_call_permit(
        response_id="response-gc",
        item_id="item-gc",
        call_id="call-gc",
        batch_id="batch-gc",
        tool_name="permitted_tool",
        arguments={},
    )
    attachment_ref = weakref.ref(gc_attachment)
    del gc_attachment
    gc.collect()
    assert attachment_ref() is None
    assert retained_permit not in invocation_module._tool_call_permit_records
