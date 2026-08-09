"""Security contract for invocation-scoped realtime attachment capture."""

from __future__ import annotations

import asyncio
import copy
import pickle
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
        chat_type="channel",
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
        chat_type="channel",
    )
    runner.session_store = MagicMock()
    runner.session_store._generate_session_key.return_value = entry.session_key
    runner.session_store.peek_session_id.return_value = entry.session_id
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
    return runner


def test_plugin_context_capture_fails_outside_gateway_dispatch():
    from gateway.realtime_voice_invocation import RealtimeVoiceInvocationError

    with pytest.raises(RealtimeVoiceInvocationError, match="active gateway plugin command"):
        _plugin_context().capture_realtime_voice_attachment_factory()


def test_capture_requires_an_existing_durable_session_mapping():
    from gateway.realtime_voice_invocation import (
        RealtimeVoiceInvocationError,
        realtime_voice_plugin_invocation,
    )

    with realtime_voice_plugin_invocation(
        source=_source(),
        routing_key="agent:main:discord:channel:voice-room",
        durable_session_id=lambda: None,
    ):
        with pytest.raises(RealtimeVoiceInvocationError, match="existing durable session"):
            _plugin_context().capture_realtime_voice_attachment_factory()


def test_exact_host_factory_is_opaque_and_lookalikes_fail_closed():
    from gateway.realtime_voice_invocation import (
        _binding_for_realtime_voice_attachment_factory,
        _is_host_realtime_voice_attachment_factory,
        realtime_voice_plugin_invocation,
    )

    source = _source()
    with realtime_voice_plugin_invocation(
        source=source,
        routing_key="route-1",
        durable_session_id=lambda: "session-1",
    ):
        factory = _plugin_context().capture_realtime_voice_attachment_factory()

    assert _is_host_realtime_voice_attachment_factory(factory)
    binding = _binding_for_realtime_voice_attachment_factory(factory)
    assert binding.routing_key == "route-1"
    assert binding.durable_session_id == "session-1"
    assert binding.principal_id == "operator"
    assert binding.chat_id == "voice-room"
    assert binding.thread_id == "thread-1"
    assert binding.scope_id == "guild-1"
    assert not hasattr(factory, "source")
    assert not hasattr(factory, "runner")

    with pytest.raises(TypeError, match="cannot be serialized"):
        copy.copy(factory)
    assert not _is_host_realtime_voice_attachment_factory(type(factory)(binding))
    with pytest.raises((pickle.PicklingError, TypeError)):
        pickle.dumps(factory)
    assert not _is_host_realtime_voice_attachment_factory(
        {"routing_key": "route-1", "durable_session_id": "session-1"}
    )


@pytest.mark.asyncio
async def test_invocation_does_not_leak_to_background_tasks_or_after_exit():
    from gateway.realtime_voice_invocation import (
        RealtimeVoiceInvocationError,
        realtime_voice_plugin_invocation,
    )

    ctx = _plugin_context()
    release = asyncio.Event()

    async def background_capture():
        await release.wait()
        return ctx.capture_realtime_voice_attachment_factory()

    with realtime_voice_plugin_invocation(
        source=_source(),
        routing_key="route-1",
        durable_session_id=lambda: "session-1",
    ):
        task = asyncio.create_task(background_capture())
        assert ctx.capture_realtime_voice_attachment_factory() is not None
        release.set()
        with pytest.raises(RealtimeVoiceInvocationError, match="dispatch task"):
            await task

    with pytest.raises(RealtimeVoiceInvocationError, match="active gateway plugin command"):
        ctx.capture_realtime_voice_attachment_factory()


@pytest.mark.asyncio
async def test_concurrent_invocations_capture_only_their_own_source():
    from gateway.realtime_voice_invocation import (
        _binding_for_realtime_voice_attachment_factory,
        realtime_voice_plugin_invocation,
    )

    ready = asyncio.Event()
    count = 0
    lock = asyncio.Lock()

    async def capture(user_id: str):
        nonlocal count
        with realtime_voice_plugin_invocation(
            source=_source(user_id=user_id, chat_id=f"room-{user_id}"),
            routing_key=f"route-{user_id}",
            durable_session_id=lambda: f"session-{user_id}",
        ):
            async with lock:
                count += 1
                if count == 2:
                    ready.set()
            await ready.wait()
            return _plugin_context().capture_realtime_voice_attachment_factory()

    first, second = await asyncio.gather(capture("one"), capture("two"))
    first_binding = _binding_for_realtime_voice_attachment_factory(first)
    second_binding = _binding_for_realtime_voice_attachment_factory(second)
    assert (first_binding.principal_id, first_binding.routing_key) == ("one", "route-one")
    assert (second_binding.principal_id, second_binding.routing_key) == ("two", "route-two")


@pytest.mark.asyncio
async def test_gateway_plugin_dispatch_exposes_factory_and_preserves_ordinary_commands(
    monkeypatch,
):
    from gateway.realtime_voice_invocation import (
        _binding_for_realtime_voice_attachment_factory,
    )
    from hermes_cli import plugins as plugins_mod

    source = _source()
    runner = _runner(source)
    ctx = _plugin_context()
    captured = []

    def handler(args: str):
        captured.append(ctx.capture_realtime_voice_attachment_factory())
        return f"joined {args}"

    monkeypatch.setattr(
        plugins_mod,
        "get_plugin_command_handler",
        lambda name: handler if name == "talk" else None,
    )

    assert await runner._handle_message(_event(source=source)) == "joined join"
    binding = _binding_for_realtime_voice_attachment_factory(captured[0])
    assert binding.routing_key == build_session_key(source)
    assert binding.durable_session_id == "durable-session-1"
    assert binding.principal_id == "operator"

    monkeypatch.setattr(
        plugins_mod,
        "get_plugin_command_handler",
        lambda name: (lambda args: f"ordinary {args}") if name == "plain" else None,
    )
    assert await runner._handle_message(_event("/plain works", source=source)) == "ordinary works"


def test_exception_restores_prior_invocation_context():
    from gateway.realtime_voice_invocation import (
        RealtimeVoiceInvocationError,
        realtime_voice_plugin_invocation,
    )

    ctx = _plugin_context()
    with pytest.raises(RuntimeError, match="handler failed"):
        with realtime_voice_plugin_invocation(
            source=_source(),
            routing_key="route-1",
            durable_session_id=lambda: "session-1",
        ):
            raise RuntimeError("handler failed")

    with pytest.raises(RealtimeVoiceInvocationError, match="active gateway plugin command"):
        ctx.capture_realtime_voice_attachment_factory()


@pytest.mark.asyncio
async def test_cancellation_resets_invocation_before_task_continues():
    from gateway.realtime_voice_invocation import (
        RealtimeVoiceInvocationError,
        realtime_voice_plugin_invocation,
    )

    ctx = _plugin_context()
    with pytest.raises(asyncio.CancelledError):
        with realtime_voice_plugin_invocation(
            source=_source(),
            routing_key="route-1",
            durable_session_id=lambda: "session-1",
        ):
            asyncio.current_task().cancel()
            await asyncio.sleep(0)

    with pytest.raises(RealtimeVoiceInvocationError, match="active gateway plugin command"):
        ctx.capture_realtime_voice_attachment_factory()
