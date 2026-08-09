"""Security contract for invocation-scoped realtime attachment capture."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import pickle
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key
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


def _event(text: str = "/talk join", *, source: SessionSource | None = None) -> MessageEvent:
    source = source or _source()
    return MessageEvent(
        text=text,
        source=source,
        user_id=source.user_id,
        message_id="message-1",
    )


def _plugin_context() -> PluginContext:
    return PluginContext(PluginManifest(name="talk"), object())


def _runner(source: SessionSource):
    from gateway.run import GatewayRunner
    from gateway.realtime_voice_invocation import _register_gateway_runner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="***")}
    )
    runner.adapters = {Platform.DISCORD: MagicMock()}
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
    runner._running_agents = {}
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

    with pytest.raises(RealtimeVoiceInvocationError, match="active opted-in plugin command"):
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
@pytest.mark.parametrize("chat_type", ["group", "thread"])
async def test_production_discord_guild_sources_can_capture(chat_type):
    from gateway.realtime_voice_invocation import _validate_realtime_voice_attachment_factory

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

    assert _validate_realtime_voice_attachment_factory(captured[0], runner).chat_type == chat_type


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
    assert not _is_host_realtime_voice_attachment_factory(
        {"routing_key": "route-1", "durable_session_id": "session-1"}
    )


@pytest.mark.asyncio
async def test_invocation_does_not_leak_to_background_tasks():
    from gateway.realtime_voice_invocation import RealtimeVoiceInvocationError

    runner, _entry = _runner(_source())

    async def handler(_args, invocation):
        task = asyncio.create_task(asyncio.to_thread(invocation.capture_realtime_voice_attachment_factory))
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
        lambda name: {"handler": handler, "invocation_context": True} if name == "talk" else None,
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
        lambda name: {
            "handler": legacy_handler,
            "invocation_context": False,
        } if name == "plain" else None,
    )
    assert await runner._handle_message(_event("/plain works", source=source)) == "ordinary works"
    assert legacy_calls == [("works",)]


@pytest.mark.asyncio
async def test_non_discord_context_opt_in_keeps_one_argument_legacy_dispatch(monkeypatch):
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
async def test_opted_handler_keeps_one_argument_gateway_compatibility(monkeypatch, platform):
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
    from gateway.realtime_voice_invocation import _is_host_realtime_voice_attachment_factory

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
    from gateway.realtime_voice_invocation import _is_host_realtime_voice_attachment_factory

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
        (SessionSource(platform=Platform.SLACK, user_id="operator", chat_id="room", scope_id="guild"), True, False),
        (_source(user_id=True), True, False),
        (_source(user_id=" operator"), True, False),
        (SessionSource(platform=Platform.DISCORD, user_id="operator", chat_id="room", scope_id=True), True, False),
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
        await _invoke(runner, source, lambda _args, _invocation: None, authenticated=authenticated, internal=internal)
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
    await _invoke(runner, source, lambda _a, inv: captured.append(inv.capture_realtime_voice_attachment_factory()))
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
        session_key=build_session_key(source), session_id="sid", created_at=datetime.now(), updated_at=datetime.now()
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
    assert store.advance_compression_session(
        route_b,
        before_compression_b[0].session_id,
        "sid-b-compressed",
    ) is before_compression_b[0]
    after_compression_b = store.get_exact_session_entry_snapshot(route_b)
    assert after_compression_b[0] is before_compression_b[0]
    assert after_compression_b[1] != before_compression_b[1]

    store.switch_session(route_a, "sid-a2")
    current_a = store.get_exact_session_entry_snapshot(route_a)
    assert current_a[0] is not captured_a[0]
    assert current_a[1] != captured_a[1]


def test_route_tombstone_survives_remove_and_recreate():
    from datetime import timedelta
    from gateway.session import SessionStore

    source = _source(chat_id="recreated-room")
    route = build_session_key(source)
    old = datetime.now() - timedelta(days=10)
    entry = SessionEntry(route, "sid-old", old, old, origin=source)
    store = object.__new__(SessionStore)
    store._entries = {route: entry}
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
    assert removed[1] != before[1]

    recreated = store._get_or_create_session_impl(source)
    after = store.get_exact_session_entry_snapshot(route)
    assert after[0] is recreated
    assert after[1] != removed[1]
