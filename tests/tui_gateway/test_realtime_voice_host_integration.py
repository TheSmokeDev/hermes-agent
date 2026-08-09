from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator

import pytest

from agent.realtime_voice_admission import RealtimeSessionBinding, RealtimeUtterance
from agent.realtime_voice_provider import (
    InputTranscript,
    RealtimeToolResult,
    RealtimeVoiceEvent,
    RealtimeVoiceProvider,
    RealtimeVoiceSession,
    RealtimeVoiceSetup,
    ToolCall,
    TranscriptProvenance,
    TranscriptRole,
)
from agent.realtime_voice_registry import _reset_for_tests, register_provider
from gateway.realtime_voice_controller import (
    ControllerLifecycle,
    GatewayRealtimeVoiceController,
    HostProjection,
    HostProjectionStatus,
)
from tui_gateway.realtime_voice_host import TuiRealtimeTurnHost, notify_realtime_turn


async def _ignore_projection(_projection: object) -> None:
    pass


class _CanarySession(RealtimeVoiceSession):
    def __init__(self) -> None:
        super().__init__(frozenset())
        self.incoming: asyncio.Queue[RealtimeVoiceEvent] = asyncio.Queue()
        self.close_calls = 0

    async def send_audio(self, audio: bytes, *, mime_type: str | None = None) -> None:
        pass

    async def _submit_tool_results(
        self, batch_id: str, results: tuple[RealtimeToolResult, ...]
    ) -> None:
        raise AssertionError("provider tools must remain inert")

    async def _events(self) -> AsyncIterator[RealtimeVoiceEvent]:
        while True:
            yield await self.incoming.get()

    async def _close(self) -> None:
        self.close_calls += 1


class _CanaryProvider(RealtimeVoiceProvider):
    def __init__(self, session: _CanarySession) -> None:
        self.session = session
        self.open_calls = 0

    @property
    def name(self) -> str:
        return "installed-canary"

    async def open_session(self, setup: RealtimeVoiceSetup) -> RealtimeVoiceSession:
        self.open_calls += 1
        return self.session


async def _eventually(predicate) -> None:
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition did not become true")


def _live_busy_session(transport: object) -> dict[str, object]:
    return {
        "history_lock": threading.Lock(),
        "session_key": "realtime-integration-durable",
        "profile_home": None,
        "transport": transport,
        "running": True,
        "queued_prompt": None,
        "_queued_prompt_generation": 0,
    }


class _CanaryTransport:
    def __init__(self) -> None:
        self.frames: list[dict[str, object]] = []

    def write(self, frame: dict[str, object]) -> bool:
        self.frames.append(frame)
        return True


def _binding(runtime_sid: str) -> RealtimeSessionBinding:
    return RealtimeSessionBinding(
        profile_id="default",
        routing_key="desktop:operator",
        runtime_session_id=runtime_sid,
        durable_session_id="realtime-integration-durable",
        provider_session_id="realtime-integration-provider",
        selection_generation=1,
    )


@pytest.mark.asyncio
async def test_installed_prompt_handler_accepts_exact_proof_and_work_survives_close() -> None:
    from tui_gateway import server

    runtime_sid = "realtime-integration-positive"
    transport = object()
    principal = object()
    session = _live_busy_session(transport)
    binding = _binding(runtime_sid)
    server._sessions[runtime_sid] = session
    try:
        host = TuiRealtimeTurnHost(
            server,
            binding,
            session=session,
            transport=transport,
            principal=principal,
            validate_principal=lambda candidate, candidate_transport, candidate_principal: (
                candidate is session and candidate_transport is transport
                and candidate_principal is principal
                ),
                projection_sink=_ignore_projection,
                )
        utterance = RealtimeUtterance(
            provider_session_id=binding.provider_session_id,
            provider_turn_id="turn-1",
            item_id="item-1",
            text="same session work",
            received_at=1.0,
        )
        permit = await host.authorize(binding, utterance)
        assert permit is not None

        receipt = await host.submit(binding, utterance, permit)

        assert receipt.status == "queued"
        assert session["queued_prompt"]["text"] == "same session work"
        assert session["queued_prompt"]["display_kind"] == "realtime_voice"
        assert isinstance(
            session["queued_prompt"]["display_metadata"]["turn_id"], str
        )
        await host.close_attachment(binding)
        assert "_realtime_voice_attachment" not in session
        assert session["queued_prompt"]["text"] == "same session work"
    finally:
        server._sessions.pop(runtime_sid, None)


@pytest.mark.parametrize("close_before_completion", [False, True])
@pytest.mark.asyncio
async def test_fake_provider_controller_real_host_installed_handler_conformance_canary(
    tmp_path, monkeypatch: pytest.MonkeyPatch, close_before_completion: bool,
) -> None:
    from agent.title_generator import maybe_auto_title
    from hermes_cli import goals
    from hermes_cli import mem_trim
    from hermes_state import SessionDB
    from run_agent import AIAgent
    from tools.process_registry import process_registry
    from tui_gateway import server

    _reset_for_tests()
    runtime_sid = "realtime-controller-conformance"
    transport = _CanaryTransport()
    principal = object()
    session = _live_busy_session(transport)
    session.update({"running": False, "history": [], "history_version": 0,
                    "attached_images": [], "cols": 80})
    binding = _binding(runtime_sid)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id=binding.durable_session_id, source="tui")
    agent = object.__new__(AIAgent)
    agent._session_db = db
    agent._session_db_created = True
    agent.session_id = binding.durable_session_id
    agent.platform = "tui"
    agent.model = "test-model"
    agent.provider = "test-provider"
    agent.base_url = ""
    agent.api_key = ""
    agent._session_messages = []
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._persist_disabled = False
    agent._cached_system_prompt = None
    agent._session_init_model_config = None
    agent._parent_session_id = None
    agent._session_json_enabled = False
    agent._session_persist_lock = threading.RLock()
    agent._db_flush_scan_prefix = []
    agent.quiet_mode = True
    agent.commit_memory_session = lambda *a, **k: None
    agent.clear_interrupt = lambda: None
    run_calls: list[str] = []
    turn_started = threading.Event()
    release_turn = threading.Event()

    def controlled_run(
        message,
        *,
        conversation_history,
        stream_callback,
        persist_user_message,
    ):
        assert message == "same runtime turn"
        assert persist_user_message == "same runtime turn"
        run_calls.append(message)
        turn_started.set()
        if close_before_completion:
            assert release_turn.wait(timeout=5)
        server._on_tool_start(runtime_sid, "tool-1", "terminal", {"command": "true"})
        stream_callback("durable answer")
        messages = [
            *conversation_history,
            {"role": "user", "content": persist_user_message},
            {"role": "assistant", "content": "durable answer"},
        ]
        AIAgent._persist_session(agent, messages, conversation_history)
        return {"messages": messages, "final_response": "durable answer"}

    agent.run_conversation = controlled_run
    session["agent"] = agent
    monkeypatch.setattr(
        "run_agent.AIAgent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a second agent/tool executor must not be constructed")
        ),
    )
    original_registry_count = len(server._sessions)
    with server._sessions_lock:
        server._sessions[runtime_sid] = session

    monkeypatch.setattr(server, "_start_agent_build", lambda *_args: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *_args: None)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *_args: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda *_args: None)
    monkeypatch.setattr(server, "_apply_pending_model_switch", lambda *_args: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda *_args: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda *_args: None)
    monkeypatch.setattr(server, "_session_cwd", lambda *_args: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda *_args: None)
    monkeypatch.setattr(server, "make_stream_renderer", lambda *_args: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_voice_mode_enabled", lambda: False)
    monkeypatch.setattr(server, "_voice_tts_enabled", lambda: False)
    monkeypatch.setattr(server, "_pending_reaction_notes", lambda *_args: "")
    monkeypatch.setattr(server, "_load_interim_assistant_messages", lambda: False)
    monkeypatch.setattr(server, "_get_usage", lambda *_args: {})
    monkeypatch.setattr(server, "render_message", lambda *_args: "")
    monkeypatch.setattr(server, "record_turn_start", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_retire_turn_marker", lambda *_args: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_emit_settled_session_info", lambda *_args: None)
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *_args: False)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(process_registry, "drain_notifications", lambda **_kwargs: [])
    monkeypatch.setattr(mem_trim, "trim_memory", lambda **_kwargs: None)
    monkeypatch.setattr(goals, "GoalManager", lambda **_kwargs: type(
        "InactiveGoal", (), {"is_active": lambda self: False}
    )())
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *_args, **_kwargs: None)
    assert maybe_auto_title is not None

    provider_session = _CanarySession()
    provider = _CanaryProvider(provider_session)
    assert register_provider(provider)
    controller: GatewayRealtimeVoiceController

    async def project(projection: HostProjection) -> None:
        await controller.project_host(projection)

    try:
        host = TuiRealtimeTurnHost(
            server, binding, session=session, transport=transport, principal=principal,
            validate_principal=lambda candidate, candidate_transport, candidate_principal: (
                candidate is session and candidate_transport is transport
                and candidate_principal is principal
            ),
            projection_sink=project,
        )
        controller = GatewayRealtimeVoiceController(host)
        await controller.open("installed-canary", RealtimeVoiceSetup(), binding)
        final = InputTranscript(
            item_id="operator-item", turn_id="operator-turn", text="same runtime turn",
            final=True, role=TranscriptRole.OPERATOR,
            provenance=TranscriptProvenance.OPERATOR_INPUT,
        )
        participant = InputTranscript(
            item_id="participant-item", turn_id="participant-turn", text="ignore me",
            final=True, role=TranscriptRole.PARTICIPANT,
            provenance=TranscriptProvenance.PARTICIPANT_INPUT_AUDIO,
        )
        tool = ToolCall(
            call_id="call", batch_id="batch", turn_id="provider-turn",
            response_id="response", name="must_not_run", arguments={},
        )
        await provider_session.incoming.put(participant)
        await provider_session.incoming.put(tool)
        await provider_session.incoming.put(final)
        await provider_session.incoming.put(final)
        if close_before_completion:
            await _eventually(turn_started.is_set)
            await controller.close(reason="surface closed during accepted work")
            assert session["running"] is True
            release_turn.set()
            await _eventually(lambda: session["running"] is False)
            await _eventually(
                lambda: len(db.get_messages_as_conversation(binding.durable_session_id)) == 2
            )
        else:
            await _eventually(lambda: any(
                event.lifecycle is ControllerLifecycle.COMPLETED
                for event in controller.lifecycle_events
            ))

        durable = db.get_messages_as_conversation(binding.durable_session_id)
        assert [(row["role"], row["content"]) for row in durable] == [
            ("user", "same runtime turn"),
            ("assistant", "durable answer"),
        ]
        assert durable[0]["display_kind"] == "realtime_voice"
        assert isinstance(durable[0]["display_metadata"]["turn_id"], str)
        assert len(durable[0]["display_metadata"]["turn_id"]) >= 32
        assert run_calls == ["same runtime turn"]
        assert provider.open_calls == 1
        assert server._sessions[runtime_sid] is session
        assert len(server._sessions) == original_registry_count + 1
        assert session["session_key"] == binding.durable_session_id
        assert session["agent"] is agent
        assert len(session["history"]) == 2
        assert sum(
            event.admission_status is not None
            and event.admission_status.value == "submitted"
            for event in controller.lifecycle_events
        ) == 1
        if not close_before_completion:
            assert [event.lifecycle for event in controller.lifecycle_events][-4:] == [
                ControllerLifecycle.THINKING,
                ControllerLifecycle.ACTING,
                ControllerLifecycle.SPEAKING,
                ControllerLifecycle.COMPLETED,
            ]

        await controller.close(reason="surface closed")
        assert session["running"] is False
        assert server._sessions[runtime_sid] is session
        assert len(server._sessions) == original_registry_count + 1
        assert provider_session.close_calls == 1
    finally:
        release_turn.set()
        with server._sessions_lock:
            server._sessions.pop(runtime_sid, None)
        db.close()
        _reset_for_tests()


@pytest.mark.asyncio
async def test_installed_prompt_handler_rejects_stale_attachment_proof_atomically() -> None:
    from tui_gateway import server

    runtime_sid = "realtime-integration-runtime"
    transport = object()
    principal = object()
    session = _live_busy_session(transport)
    binding = _binding(runtime_sid)
    server._sessions[runtime_sid] = session
    try:
        TuiRealtimeTurnHost(
            server,
            binding,
            session=session,
            transport=transport,
            principal=principal,
            validate_principal=lambda candidate, candidate_transport, candidate_principal: (
                candidate is session and candidate_transport is transport
                and candidate_principal is principal
                ),
                projection_sink=_ignore_projection,
                )
        attachment = session["_realtime_voice_attachment"]
        assert isinstance(attachment, dict)
        proof = attachment["capability"]
        session["_realtime_voice_attachment"] = {
            **attachment,
            "capability": object(),
        }

        response = server._methods["prompt.submit"](
            "stale-realtime-proof",
            {
                "session_id": runtime_sid,
                "text": "must not queue",
                "queued": True,
                "_trusted_realtime_attachment": proof,
            },
        )

        assert response["error"]["code"] == 4013
        assert session["queued_prompt"] is None

        # A shallow-copied replacement carrying the exact attachment dict must
        # still fail at the installed handler's busy/idle linearization lock.
        session["_realtime_voice_attachment"] = attachment
        replacement = dict(session)
        replacement["history_lock"] = threading.Lock()
        server._sessions[runtime_sid] = replacement
        response = server._methods["prompt.submit"](
            "replacement-runtime-record",
            {
                "session_id": runtime_sid,
                "text": "must not queue replacement",
                "queued": True,
                "_trusted_realtime_attachment": attachment["capability"],
            },
        )
        assert response["error"]["code"] == 4013
        assert replacement["queued_prompt"] is None
    finally:
        server._sessions.pop(runtime_sid, None)


@pytest.mark.parametrize("running", [False, True])
def test_registry_replacement_after_lookup_cannot_claim_or_enqueue(
    monkeypatch: pytest.MonkeyPatch, running: bool,
) -> None:
    from tui_gateway import server

    runtime_sid = f"realtime-lookup-race-{running}"
    transport = object()
    principal = object()
    session = _live_busy_session(transport)
    session["running"] = running
    binding = _binding(runtime_sid)
    with server._sessions_lock:
        server._sessions[runtime_sid] = session
    TuiRealtimeTurnHost(
        server, binding, session=session, transport=transport, principal=principal,
        validate_principal=lambda candidate, candidate_transport, candidate_principal: (
            candidate is session and candidate_transport is transport
            and candidate_principal is principal
        ),
        projection_sink=_ignore_projection,
    )
    attachment = session["_realtime_voice_attachment"]
    assert isinstance(attachment, dict)

    original_sess_nowait = server._sess_nowait

    def replace_after_lookup(params, rid):
        selected, error = original_sess_nowait(params, rid)
        sid = params.get("session_id", "")
        with server._sessions_lock:
            server._sessions[sid] = {**selected, "history_lock": threading.Lock()}
        return selected, error

    monkeypatch.setattr(server, "_sess_nowait", replace_after_lookup)
    try:
        response = server._methods["prompt.submit"](
            "lookup-race",
            {
                "session_id": runtime_sid,
                "text": "must not mutate stale record",
                "queued": True,
                "_trusted_realtime_attachment": attachment["capability"],
            },
        )
        assert response["error"]["code"] == 4013
        assert session["running"] is running
        assert session["queued_prompt"] is None
    finally:
        with server._sessions_lock:
            server._sessions.pop(runtime_sid, None)


def test_private_realtime_stop_bypasses_global_typed_stop_before_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tui_gateway import server

    runtime_sid = "realtime-private-stop"
    transport = object()
    principal = object()
    session = _live_busy_session(transport)
    binding = _binding(runtime_sid)
    with server._sessions_lock:
        server._sessions[runtime_sid] = session
    TuiRealtimeTurnHost(
        server, binding, session=session, transport=transport, principal=principal,
        validate_principal=lambda *_args: True,
        projection_sink=_ignore_projection,
    )
    stopped: list[str] = []
    monkeypatch.setattr(server, "_voice_mode_enabled", lambda: True)
    monkeypatch.setattr(server, "_tts_stream_stop", lambda **_kwargs: stopped.append("tts"))
    monkeypatch.setattr(server, "_voice_emit", lambda *_args: stopped.append("emit"))
    session["_realtime_voice_attachment"] = {}
    try:
        response = server._methods["prompt.submit"](
            "private-stop",
            {
                "session_id": runtime_sid,
                "text": "stop",
                "queued": True,
                "_trusted_realtime_attachment": object(),
            },
        )
        assert response["error"]["code"] == 4013
        assert stopped == []
    finally:
        with server._sessions_lock:
            server._sessions.pop(runtime_sid, None)


@pytest.mark.asyncio
async def test_queued_generation_invalidation_releases_accepted_turn_marker() -> None:
    from tui_gateway import server

    runtime_sid = "realtime-queued-generation-invalid"
    transport = object()
    principal = object()
    session = _live_busy_session(transport)
    session["running"] = False
    session["_queued_prompt_generation"] = 2
    binding = _binding(runtime_sid)
    with server._sessions_lock:
        server._sessions[runtime_sid] = session
    try:
        host = TuiRealtimeTurnHost(
            server,
            binding,
            session=session,
            transport=transport,
            principal=principal,
            validate_principal=lambda candidate, candidate_transport, candidate_principal: (
                candidate is session
                and candidate_transport is transport
                and candidate_principal is principal
            ),
            projection_sink=_ignore_projection,
        )
        host._canonical_work_pending = True
        host._active_turn_id = "queued-turn"
        session["queued_prompt"] = {
            "text": "never starts",
            "transport": transport,
            "display_kind": "realtime_voice",
            "display_metadata": {"turn_id": "queued-turn"},
        }

        class GenerationRaceLock:
            def __init__(self) -> None:
                self._lock = threading.RLock()
                self.exits = 0

            def __enter__(self):
                self._lock.acquire()
                return self

            def __exit__(self, *_args):
                self._lock.release()
                self.exits += 1
                if self.exits == 1:
                    session["_queued_prompt_generation"] += 1

        session["history_lock"] = GenerationRaceLock()
        assert server._drain_queued_prompt(
            "queued-generation-invalid", runtime_sid, session
        )

        assert session["running"] is False
        assert host._canonical_work_pending is False
        assert host._active_turn_id is None
        assert await host.authorize(
            binding,
            RealtimeUtterance(
                provider_session_id=binding.provider_session_id,
                provider_turn_id="next-turn",
                item_id="next-item",
                text="next accepted utterance",
                received_at=2.0,
            ),
        ) is not None
    finally:
        with server._sessions_lock:
            server._sessions.pop(runtime_sid, None)


@pytest.mark.asyncio
async def test_compute_host_flip_fails_and_releases_marker_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tui_gateway import server

    runtime_sid = "realtime-compute-flip"
    transport = object()
    principal = object()
    session = _live_busy_session(transport)
    session["running"] = False
    binding = _binding(runtime_sid)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_args: False)
    with server._sessions_lock:
        server._sessions[runtime_sid] = session
    try:
        host = TuiRealtimeTurnHost(
            server,
            binding,
            session=session,
            transport=transport,
            principal=principal,
            validate_principal=lambda *_args: True,
            projection_sink=_ignore_projection,
        )
        host._canonical_work_pending = True
        host._active_turn_id = "compute-flip-turn"
        original_attachment = session["_realtime_voice_attachment"]
        session["_realtime_voice_attachment"] = dict(original_attachment)
        assert not notify_realtime_turn(
            session,
            HostProjectionStatus.FAILED,
            detail="replacement must not terminate captured attachment",
            allow_host_drift=True,
        )
        assert host._canonical_work_pending is True
        assert host._active_turn_id == "compute-flip-turn"
        session["_realtime_voice_attachment"] = original_attachment
        session["queued_prompt"] = {
            "text": "must not enter compute host",
            "transport": transport,
            "display_kind": "realtime_voice",
            "display_metadata": {"turn_id": "compute-flip-turn"},
        }
        monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_args: True)
        monkeypatch.setattr(
            server,
            "_submit_prompt_to_compute_host",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("realtime work must not dispatch to compute host")
            ),
        )

        assert server._drain_queued_prompt("compute-flip", runtime_sid, session)
        assert session["running"] is False
        assert host._canonical_work_pending is False
        assert host._active_turn_id is None
    finally:
        with server._sessions_lock:
            server._sessions.pop(runtime_sid, None)
