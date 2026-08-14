"""Security contract for invocation-scoped realtime attachment capture."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import copy
import dataclasses
import json
import pickle
import threading
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
