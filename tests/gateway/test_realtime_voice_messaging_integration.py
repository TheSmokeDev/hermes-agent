from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from agent.realtime_voice_admission import RealtimeSessionBinding, RealtimeUtterance
from agent.realtime_voice_provider import (
    InputTranscript,
    RealtimeToolResult,
    RealtimeVoiceEvent,
    RealtimeVoiceProvider,
    RealtimeVoiceSession,
    RealtimeVoiceSetup,
    SessionReady,
    ToolCall,
    TranscriptProvenance,
    TranscriptRole,
)
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key


def _discord_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="222222222222222222",
        chat_type="group",
        user_id="111111111111111111",
        thread_id="333333333333333333",
        scope_id="444444444444444444",
        is_bot=False,
    )


async def _capture_factory(runner, source: SessionSource, route: str):
    from gateway.realtime_voice_invocation import _invoke_plugin_command_with_context

    captured = []
    await _invoke_plugin_command_with_context(
        runner=runner,
        handler=lambda _args, invocation: captured.append(
            invocation.capture_realtime_voice_attachment_factory()
        ),
        raw_args="core join",
        source=source,
        routing_key=route,
        authenticated=True,
        internal=False,
    )
    return captured[0]


class _InstalledSession(RealtimeVoiceSession):
    def __init__(self) -> None:
        super().__init__(frozenset())
        self.incoming: asyncio.Queue[RealtimeVoiceEvent] = asyncio.Queue()
        self.close_calls = 0
        self.tool_result_calls = 0

    async def send_audio(self, audio: bytes, *, mime_type: str | None = None) -> None:
        pass

    async def _submit_tool_results(
        self, batch_id: str, results: tuple[RealtimeToolResult, ...]
    ) -> None:
        self.tool_result_calls += 1
        raise AssertionError("provider tools must remain inert")

    async def _events(self) -> AsyncIterator[RealtimeVoiceEvent]:
        while True:
            yield await self.incoming.get()

    async def _close(self) -> None:
        self.close_calls += 1


class _InstalledProvider(RealtimeVoiceProvider):
    def __init__(self, session: _InstalledSession) -> None:
        self.session = session
        self.open_calls = 0

    @property
    def name(self) -> str:
        return "installed-messaging-canary"

    async def open_session(self, setup: RealtimeVoiceSetup) -> RealtimeVoiceSession:
        self.open_calls += 1
        return self.session


@pytest.mark.asyncio
async def test_installed_factory_controller_real_gateway_canonical_ingress_survives_close(
    monkeypatch, tmp_path
):
    """The installed invocation factory reaches the real durable gateway path."""
    import gateway.run as gateway_run
    import hermes_state
    import run_agent
    from agent.realtime_voice_registry import _reset_for_tests, register_provider
    from gateway.realtime_voice_controller import GatewayRealtimeVoiceController
    from gateway.realtime_voice_messaging_host import (
        _ACCEPTED_TASKS,
        _CLAIM_ATTR,
        _MARKER_KEY,
        RealtimeVoiceFinalizationReceipt,
    )
    from gateway.session import AsyncSessionStore, SessionStore
    from gateway.turn_lease import SessionTurnLeaseRegistry
    from hermes_state import AsyncSessionDB, SessionDB
    from tests.gateway.test_42039_duplicate_user_message import _bootstrap

    _reset_for_tests()
    runner = _bootstrap(monkeypatch, tmp_path)
    # The broad bootstrap constructs production objects before narrowing them
    # for its legacy tests. Close those handles, then install one real DB shared
    # by the real SessionStore and runner exactly as production does.
    closed: set[int] = set()
    for candidate in (
        getattr(runner.session_store, "_db", None),
        getattr(getattr(runner, "_session_db", None), "_db", None),
    ):
        if candidate is not None and id(candidate) not in closed:
            candidate.close()
            closed.add(id(candidate))

    db = SessionDB(db_path=tmp_path / "installed-state.db")
    real_session_db_type = hermes_state.SessionDB
    monkeypatch.setattr(hermes_state, "SessionDB", lambda: db)
    store = SessionStore(tmp_path / "sessions", runner.config)
    monkeypatch.setattr(hermes_state, "SessionDB", real_session_db_type)

    source = _discord_source()
    route = build_session_key(source)
    now = datetime.now()
    entry = SessionEntry(
        session_key=route,
        session_id="durable-installed-realtime",
        created_at=now,
        updated_at=now,
        platform=Platform.DISCORD,
        chat_type="group",
    )
    db.create_session(session_id=entry.session_id, source="discord")
    store._entries[route] = entry
    store._loaded = True
    store._route_structural_generations[route] = 1
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = AsyncSessionDB(db)
    runner._turn_leases = SessionTurnLeaseRegistry()
    runner._turn_lease_tokens = {}
    runner._external_drain_active = False
    runner._persist_active_agents = MagicMock()

    captured_events: list[MessageEvent] = []

    def capture_pre_dispatch(_hook_name, **kwargs):
        captured_events.append(kwargs["event"])
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", capture_pre_dispatch)

    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    run_calls: list[str] = []

    async def controlled_model_io(**kwargs):
        message = kwargs["message"]
        assert kwargs["session_id"] == entry.session_id
        assert kwargs["session_key"] == route
        run_calls.append(message)
        handler_started.set()
        await release_handler.wait()
        return {
            "final_response": "canonical installed response",
            "messages": [
                {"role": "user", "content": message},
                {"role": "assistant", "content": "canonical installed response"},
            ],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "agent_persisted": False,
        }

    # This is the only patched production seam: external model execution. The
    # real _handle_message, SessionStore, turn lease, SessionDB, factory,
    # controller, registry, and provider event pump all remain installed.
    runner._run_agent = controlled_model_io

    constructor_calls = {"controller": 0}
    real_controller_init = GatewayRealtimeVoiceController.__init__

    def counted_controller_init(self, *args, **kwargs):
        constructor_calls["controller"] += 1
        real_controller_init(self, *args, **kwargs)

    monkeypatch.setattr(
        GatewayRealtimeVoiceController, "__init__", counted_controller_init
    )
    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "__init__",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a second GatewayRunner must not be constructed")
        ),
    )
    monkeypatch.setattr(
        run_agent.AIAgent,
        "__init__",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a second AIAgent/legacy executor must not be constructed")
        ),
    )
    monkeypatch.setattr(
        real_session_db_type,
        "__init__",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a second SessionDB must not be constructed")
        ),
    )

    session = _InstalledSession()
    provider = _InstalledProvider(session)
    assert register_provider(provider)
    factory = await _capture_factory(runner, source, route)
    attachment = await factory.open(
        provider.name,
        RealtimeVoiceSetup(),
        provider_session_id="provider-installed-session",
    )
    tool = ToolCall(
        call_id="provider-call",
        batch_id="provider-batch",
        turn_id="provider-tool-turn",
        response_id="provider-response",
        name="must_not_execute",
        arguments={},
    )
    final = InputTranscript(
        item_id="provider-final-item",
        turn_id="provider-final-turn",
        text="installed voice turn",
        final=True,
        role=TranscriptRole.OPERATOR,
        provenance=TranscriptProvenance.OPERATOR_INPUT,
    )

    try:
        await session.incoming.put(SessionReady(session_id="provider-native"))
        await session.incoming.put(tool)
        await session.incoming.put(final)
        await asyncio.wait_for(handler_started.wait(), timeout=5)

        # Closing revokes only unconsumed attachment authority. The exact turn
        # already accepted by the canonical handler remains host-owned.
        await asyncio.wait_for(attachment.close(), timeout=5)
        assert runner._is_session_running(route)
        release_handler.set()
        accepted = tuple(_ACCEPTED_TASKS)
        assert len(accepted) == 1
        results = await asyncio.wait_for(asyncio.gather(*accepted), timeout=5)
        # The retained canonical task is the response-delivery boundary used by
        # this direct installed host ingress. It returns exactly once even though
        # the controller waiter was cancelled by attachment close.
        assert results == ["canonical installed response"]

        rows = db.get_messages(entry.session_id, include_inactive=True)
        user_rows = [row for row in rows if row["role"] == "user"]
        assistant_rows = [row for row in rows if row["role"] == "assistant"]
        assert [row["content"] for row in user_rows] == ["installed voice turn"]
        assert [row["content"] for row in assistant_rows] == [
            "canonical installed response"
        ]
        user_row = user_rows[0]
        assistant_row = assistant_rows[0]
        assert user_row["id"] < assistant_row["id"]
        assert user_row["display_kind"] == "realtime_voice_turn"
        marker = user_row["display_metadata"][_MARKER_KEY]
        assert type(marker) is str and len(marker) == 32

        claim = getattr(captured_events[-1], _CLAIM_ATTR)
        receipt = claim.receipt
        assert type(receipt) is RealtimeVoiceFinalizationReceipt
        assert receipt.turn_marker == marker
        assert receipt.user_message_id == user_row["id"]
        assert receipt.assistant_message_id == assistant_row["id"]
        assert claim.host.validate_finalization(receipt)
        reread = db.get_messages(entry.session_id, include_inactive=True)
        reread_user = next(row for row in reread if row["id"] == user_row["id"])
        assert reread_user["display_kind"] == "realtime_voice_turn"
        assert reread_user["display_metadata"][_MARKER_KEY] == marker

        assert run_calls == ["installed voice turn"]
        assert provider.open_calls == 1
        assert session.tool_result_calls == 0
        assert session.close_calls == 1
        assert constructor_calls == {"controller": 1}
        assert not runner._is_session_running(route)
        assert runner._turn_lease_tokens == {}
        assert not _ACCEPTED_TASKS
        controller = attachment._controller
        assert controller._event_task.done()
        assert controller._audio_task.done()
        assert controller._closed is True
    finally:
        release_handler.set()
        await attachment.close()
        db.close()
        _reset_for_tests()


@pytest.mark.asyncio
async def test_realtime_idle_claim_is_linearized_at_actual_running_slot(
    monkeypatch, tmp_path
):
    """A raced ordinary claim wins without any realtime slot mutation."""
    from gateway.realtime_voice_messaging_host import (
        RealtimeVoiceIngressError,
        _create_messaging_host,
    )
    from gateway.turn_lease import SessionTurnLeaseRegistry
    from tests.gateway.test_42039_duplicate_user_message import _bootstrap

    runner = _bootstrap(monkeypatch, tmp_path)
    source = _discord_source()
    route = build_session_key(source)
    entry = SessionEntry(
        session_key=route,
        session_id="durable-realtime-race",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.DISCORD,
        chat_type="group",
    )
    runner.session_store.get_exact_session_entry_snapshot.return_value = (entry, 7)
    runner.session_store.get_or_create_session.return_value = entry
    runner._turn_leases = SessionTurnLeaseRegistry()
    runner._turn_lease_tokens = {}
    runner._external_drain_active = False
    runner._persist_active_agents = MagicMock()
    run_generations = 0

    def begin_run_generation(_route: str) -> int:
        nonlocal run_generations
        run_generations += 1
        return run_generations

    runner._begin_session_run_generation = begin_run_generation

    factory = await _capture_factory(runner, source, route)
    host = _create_messaging_host(factory, runner)
    binding = RealtimeSessionBinding(
        profile_id="default",
        routing_key=route,
        runtime_session_id=route,
        durable_session_id=entry.session_id,
        provider_session_id="provider-race",
        selection_generation=7,
    )
    utterance = RealtimeUtterance(
        provider_session_id="provider-race",
        provider_turn_id="turn-race",
        item_id="item-race",
        text="voice loses the raced slot",
        received_at=1.0,
    )
    permit = await host.authorize(binding, utterance)
    assert permit is not None

    realtime_at_gate = threading.Event()
    release_realtime_gate = threading.Event()
    real_lobby_check = runner._is_telegram_topic_root_lobby

    def gated_lobby_check(candidate: SessionSource) -> bool:
        if candidate is not source:
            realtime_at_gate.set()
            assert release_realtime_gate.wait(timeout=5)
        return real_lobby_check(candidate)

    runner._is_telegram_topic_root_lobby = gated_lobby_check
    ordinary_entered = asyncio.Event()
    release_ordinary = asyncio.Event()
    realtime_entered = asyncio.Event()
    ordinary_agent = object()

    async def gated_handler(event, _source, _route, _generation):
        if event.text == utterance.text:
            realtime_entered.set()
            raise AssertionError("busy realtime work entered the canonical agent path")
        state = runner._session_state(route)
        state.turn.agent = ordinary_agent
        ordinary_entered.set()
        await release_ordinary.wait()
        return {"final_response": "ordinary response"}

    runner._handle_message_with_agent = gated_handler
    realtime_task = asyncio.create_task(host.submit(binding, utterance, permit))
    assert await asyncio.to_thread(realtime_at_gate.wait, 5)

    ordinary_event = MessageEvent(
        text="ordinary turn owns the route",
        message_type=MessageType.TEXT,
        source=source,
        user_id=source.user_id,
        metadata={},
    )
    ordinary_task = asyncio.create_task(runner._handle_message(ordinary_event))
    await asyncio.wait_for(ordinary_entered.wait(), timeout=5)

    state = runner._session_state(route)
    ordinary_started_ts = state.turn.started_ts
    ordinary_generation = run_generations
    ordinary_lease = state.turn.lease
    ordinary_lease_token = state.turn.lease_token
    assert state.turn.agent is ordinary_agent

    release_realtime_gate.set()
    with pytest.raises(RealtimeVoiceIngressError, match="busy"):
        await asyncio.wait_for(realtime_task, timeout=5)

    state = runner._session_state(route)
    assert not realtime_entered.is_set()
    assert state.turn.agent is ordinary_agent
    assert state.turn.started_ts == ordinary_started_ts
    assert run_generations == ordinary_generation
    assert state.turn.lease is ordinary_lease
    assert state.turn.lease_token is ordinary_lease_token
    assert runner._pending_messages == {}

    release_ordinary.set()
    assert await asyncio.wait_for(ordinary_task, timeout=5) == {
        "final_response": "ordinary response"
    }
    assert not runner._is_session_running(route)
    assert runner._turn_lease_tokens == {}
