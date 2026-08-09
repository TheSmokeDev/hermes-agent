from __future__ import annotations

import asyncio
import threading
from dataclasses import FrozenInstanceError


import pytest

from agent.realtime_voice_admission import RealtimeSessionBinding, RealtimeUtterance
from gateway.realtime_voice_controller import HostProjectionStatus
from tui_gateway.realtime_voice_host import (
    RealtimeTurnHostError,
    TuiRealtimeTurnHost,
    notify_realtime_turn,
)


async def _ignore_projection(_projection: object) -> None:
    pass


class _CountingLock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.entries = 0

    def __enter__(self) -> None:
        self._lock.acquire()
        self.entries += 1

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()


class _Server:
    def __init__(self, session: dict[str, object]) -> None:
        self._sessions = {"runtime-session": session}
        self.prompt_calls: list[tuple[object, dict[str, object]]] = []
        self.interrupt_calls: list[tuple[object, dict[str, object]]] = []
        self.prompt_response: object = {
            "jsonrpc": "2.0",
            "id": "realtime-voice",
            "result": {"status": "streaming"},
        }
        self.interrupt_response: object = {
            "jsonrpc": "2.0",
            "id": "realtime-voice",
            "result": {"status": "interrupted"},
        }
        self.prompt_exception: BaseException | None = None

        def prompt_submit(rid: object, params: dict[str, object]) -> object:
            self.prompt_calls.append((rid, dict(params)))
            if self.prompt_exception is not None:
                raise self.prompt_exception
            return self.prompt_response

        def session_interrupt(rid: object, params: dict[str, object]) -> object:
            self.interrupt_calls.append((rid, dict(params)))
            return self.interrupt_response

        self._methods = {
            "prompt.submit": prompt_submit,
            "session.interrupt": session_interrupt,
        }


def _binding(**changes: object) -> RealtimeSessionBinding:
    values: dict[str, object] = {
        "profile_id": "default",
        "routing_key": "desktop:operator",
        "runtime_session_id": "runtime-session",
        "durable_session_id": "durable-session",
        "provider_session_id": "provider-session",
        "selection_generation": 7,
    }
    values.update(changes)
    return RealtimeSessionBinding(**values)  # type: ignore[arg-type]


def _utterance(**changes: object) -> RealtimeUtterance:
    values: dict[str, object] = {
        "provider_session_id": "provider-session",
        "provider_turn_id": "turn-1",
        "item_id": "item-1",
        "text": "check the date",
        "received_at": 42.5,
    }
    values.update(changes)
    return RealtimeUtterance(**values)  # type: ignore[arg-type]


def _host() -> tuple[TuiRealtimeTurnHost, _Server, dict[str, object], object, object]:
    transport = object()
    principal = object()
    session: dict[str, object] = {
        "history_lock": threading.Lock(),
        "session_key": "durable-session",
        "profile_home": None,
        "transport": transport,
        "running": False,
    }
    server = _Server(session)
    host = TuiRealtimeTurnHost(
        server,
        _binding(),
        session=session,
        transport=transport,
        principal=principal,
        validate_principal=lambda candidate, candidate_transport, candidate_principal: (
            candidate is session and candidate_transport is transport
            and candidate_principal is principal
        ),
        projection_sink=_ignore_projection,
    )
    return host, server, session, transport, principal


def test_attachment_captures_exact_host_capability_and_event_loop() -> None:
    async def build() -> tuple[
        TuiRealtimeTurnHost, dict[str, object], asyncio.AbstractEventLoop
    ]:
        host, _server, session, _transport, _principal = _host()
        return host, session, asyncio.get_running_loop()

    host, session, loop = asyncio.run(build())
    attachment = session["_realtime_voice_attachment"]
    assert isinstance(attachment, dict)
    assert attachment["host"] is host
    assert attachment["loop"] is loop
    assert attachment["capability"] is not host
    assert not hasattr(host, "project_turn")


@pytest.mark.asyncio
async def test_production_notifications_are_ordered_and_unpersisted_completion_fails() -> None:
    projections = []

    async def sink(projection) -> None:
        projections.append(projection)

    host, _server, session, _transport, _principal = _host()
    host._projection_sink = sink
    host._canonical_work_pending = True
    host._active_turn_id = "current-turn"
    missing_db_agent = type(
        "MissingDurableTail",
        (),
        {
            "session_id": "durable-session",
            "_session_db": type(
                "EmptyDB", (), {"get_messages_as_conversation": lambda *_args: []}
            )(),
        },
    )()

    assert notify_realtime_turn(session, HostProjectionStatus.THINKING)
    assert not notify_realtime_turn(session, HostProjectionStatus.THINKING)
    assert notify_realtime_turn(session, HostProjectionStatus.SPEAKING)
    assert not notify_realtime_turn(session, HostProjectionStatus.ACTING)
    assert notify_realtime_turn(
        session,
        HostProjectionStatus.COMPLETED,
        agent=missing_db_agent,
        expected_user="question",
        expected_assistant="answer",
    )
    for _ in range(20):
        if len(projections) == 3:
            break
        await asyncio.sleep(0)

    assert [projection.status for projection in projections] == [
        HostProjectionStatus.THINKING,
        HostProjectionStatus.SPEAKING,
        HostProjectionStatus.FAILED,
    ]
    assert projections[-1].finalization is None
    assert await host.authorize(_binding(), _utterance()) is not None


@pytest.mark.asyncio
async def test_tool_interleaved_durable_completion_mints_consume_once_receipt() -> None:
    projections = []

    async def sink(projection) -> None:
        projections.append(projection)

    host, _server, session, _transport, _principal = _host()
    host._projection_sink = sink
    host._canonical_work_pending = True
    host._active_turn_id = "current-turn"
    messages = [
        {
            "role": "user",
            "content": "question",
            "display_kind": "realtime_voice",
            "display_metadata": {"turn_id": "current-turn"},
        },
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "content": "tool result", "tool_call_id": "call-1"},
        {"role": "assistant", "content": "answer"},
    ]
    agent = type(
        "DurableToolTurn",
        (),
        {
            "session_id": "durable-session",
            "_session_db": type(
                "DurableDB",
                (),
                {"get_messages_as_conversation": lambda *_args: messages},
            )(),
        },
    )()

    assert notify_realtime_turn(session, HostProjectionStatus.THINKING)
    assert notify_realtime_turn(
        session,
        HostProjectionStatus.COMPLETED,
        agent=agent,
        expected_user="question",
        expected_assistant="answer",
    )
    for _ in range(20):
        if len(projections) == 2:
            break
        await asyncio.sleep(0)

    receipt = projections[-1].finalization
    assert receipt is not None
    assert host.validate_finalization(receipt)
    assert not host.validate_finalization(receipt)


@pytest.mark.asyncio
async def test_older_identical_exchange_cannot_finalize_current_unpersisted_turn() -> None:
    projections = []

    async def sink(projection) -> None:
        projections.append(projection)

    host, _server, session, _transport, _principal = _host()
    host._projection_sink = sink
    host._canonical_work_pending = True
    host._active_turn_id = "current-turn"
    old_messages = [
        {"role": "user", "content": "same question"},
        {"role": "assistant", "content": "same answer"},
    ]
    agent = type(
        "OldIdenticalTurnOnly",
        (),
        {
            "session_id": "durable-session",
            "_session_db": type(
                "OldOnlyDB",
                (),
                {"get_messages_as_conversation": lambda *_args: old_messages},
            )(),
        },
    )()

    assert notify_realtime_turn(
        session,
        HostProjectionStatus.COMPLETED,
        agent=agent,
        expected_user="same question",
        expected_assistant="same answer",
    )
    for _ in range(20):
        if projections:
            break
        await asyncio.sleep(0)

    assert projections[-1].status is HostProjectionStatus.FAILED
    assert projections[-1].finalization is None


def _replace_drift_value(
    session: dict[str, object], field: str, changed: object
) -> object:
    if field.startswith("attachment."):
        attachment = session["_realtime_voice_attachment"]
        assert isinstance(attachment, dict)
        key = field.removeprefix("attachment.")
        original = attachment[key]
        attachment[key] = changed
        return original
    original = session[field]
    session[field] = changed
    return original


@pytest.mark.asyncio
async def test_real_tui_session_schema_installs_namespaced_attachment_capability() -> None:
    transport = object()
    principal = object()
    session: dict[str, object] = {
        "history_lock": threading.Lock(),
        "session_key": "durable-session",
        "profile_home": None,
        "transport": transport,
        "running": False,
    }
    server = _Server(session)

    host = TuiRealtimeTurnHost(
        server,
        _binding(),
        session=session,
        transport=transport,
        principal=principal,
        validate_principal=lambda candidate, candidate_transport, candidate_principal: (
            candidate is session and candidate_transport is transport
            and candidate_principal is principal
        ),
        projection_sink=_ignore_projection,
    )

    attachment = session.get("_realtime_voice_attachment")
    assert isinstance(attachment, dict)
    assert attachment["selection_generation"] == 7
    assert attachment["principal"] is principal
    assert attachment["transport"] is transport
    assert await host.authorize(_binding(), _utterance()) is not None


def test_constructor_requires_independent_principal_validator() -> None:
    transport = object()
    session: dict[str, object] = {"history_lock": threading.Lock(), "session_key": "durable-session",
        "profile_home": None, "transport": transport, "running": False}
    with pytest.raises(TypeError):
        TuiRealtimeTurnHost(_Server(session), _binding(), session=session,
            transport=transport, principal=object())  # type: ignore[call-arg]


def test_constructor_rejects_process_isolated_compute_host_session() -> None:
    transport = object()
    principal = object()
    session: dict[str, object] = {
        "history_lock": threading.Lock(),
        "session_key": "durable-session",
        "profile_home": None,
        "transport": transport,
        "running": False,
    }
    server = _Server(session)
    server._session_uses_compute_host = lambda candidate: candidate is session

    with pytest.raises(ValueError, match="does not match the live TUI session"):
        TuiRealtimeTurnHost(
            server,
            _binding(),
            session=session,
            transport=transport,
            principal=principal,
            validate_principal=lambda *_args: True,
            projection_sink=_ignore_projection,
        )
    assert "_realtime_voice_attachment" not in session


@pytest.mark.asyncio
async def test_live_principal_revalidation_denies_each_operation() -> None:
    transport = object()
    principal = object()
    session: dict[str, object] = {"history_lock": threading.Lock(), "session_key": "durable-session",
        "profile_home": None, "transport": transport, "running": False}
    authenticated = True
    host = TuiRealtimeTurnHost(_Server(session), _binding(), session=session, transport=transport,
        principal=principal, validate_principal=lambda *_args: authenticated,
        projection_sink=_ignore_projection)
    permit = await host.authorize(_binding(), _utterance())
    assert permit is not None
    authenticated = False
    with pytest.raises(RealtimeTurnHostError, match="permit"):
        await host.submit(_binding(), _utterance(), permit)
    assert await host.authorize(_binding(), _utterance(item_id="item-2")) is None


def test_concurrent_attachment_displacement_is_refused() -> None:
    host, server, session, transport, principal = _host()
    assert host is not None
    with pytest.raises(ValueError, match="already attached"):
        TuiRealtimeTurnHost(server, _binding(), session=session, transport=transport,
            principal=principal, validate_principal=lambda *_args: True,
            projection_sink=_ignore_projection)


@pytest.mark.asyncio
async def test_exact_permit_schedules_installed_prompt_handler_once() -> None:
    host, server, _session, _transport, _principal = _host()
    binding = _binding()
    utterance = _utterance()

    permit = await host.authorize(binding, utterance)
    assert permit is not None
    receipt = await host.submit(binding, utterance, permit)

    assert len(server.prompt_calls) == 1
    request_id, params = server.prompt_calls[0]
    attachment = _session["_realtime_voice_attachment"]
    assert isinstance(attachment, dict)
    assert params.pop("_trusted_realtime_attachment") is attachment["capability"]
    assert params.pop("_trusted_realtime_turn_id") == host._active_turn_id
    assert (request_id, params) == (
        "realtime-voice",
        {
            "session_id": "runtime-session",
            "text": "check the date",
            "queued": True,
        },
    )
    assert receipt.status == "streaming"
    assert receipt.binding == binding
    assert receipt.provider_turn_id == "turn-1"
    assert receipt.item_id == "item-1"
    with pytest.raises(FrozenInstanceError):
        receipt.status = "queued"  # type: ignore[misc]

    with pytest.raises(RealtimeTurnHostError, match="permit"):
        await host.submit(binding, utterance, permit)
    assert len(server.prompt_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("session_key", "rotated-session"),
        ("profile_home", "C:/profiles/work"),
        ("transport", object()),
        ("attachment.principal", object()),
        ("attachment.selection_generation", 8),
    ],
)
async def test_each_live_attachment_drift_dimension_denies_authorization(
    field: str, changed: object
) -> None:
    host, server, session, _transport, _principal = _host()
    _replace_drift_value(session, field, changed)

    assert await host.authorize(_binding(), _utterance()) is None
    assert server.prompt_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("registry_change", ["removed", "replaced"])
async def test_runtime_record_removal_or_identity_replacement_denies_authorization(
    registry_change: str,
) -> None:
    host, server, session, _transport, _principal = _host()
    if registry_change == "removed":
        del server._sessions["runtime-session"]
    else:
        server._sessions["runtime-session"] = dict(session)

    assert await host.authorize(_binding(), _utterance()) is None
    assert server.prompt_calls == []


@pytest.mark.asyncio
async def test_binding_or_provider_identity_mismatch_denies_authorization() -> None:
    host, server, _session, _transport, _principal = _host()

    assert await host.authorize(_binding(routing_key="desktop:other"), _utterance()) is None
    assert (
        await host.authorize(
            _binding(), _utterance(provider_session_id="other-provider-session")
        )
        is None
    )
    assert server.prompt_calls == []


@pytest.mark.asyncio
async def test_wrong_utterance_foreign_host_and_revoked_permits_fail_closed() -> None:
    host, server, _session, _transport, _principal = _host()
    other_host, _other_server, _other_session, _other_transport, _other_principal = _host()
    binding = _binding()
    utterance = _utterance()
    wrong_utterance = _utterance(item_id="item-2")

    wrong_permit = await host.authorize(binding, utterance)
    assert wrong_permit is not None
    with pytest.raises(RealtimeTurnHostError, match="permit"):
        await host.submit(binding, wrong_utterance, wrong_permit)

    foreign_permit = await other_host.authorize(binding, utterance)
    assert foreign_permit is not None
    with pytest.raises(RealtimeTurnHostError, match="permit"):
        await host.submit(binding, utterance, foreign_permit)

    revoked_permit = await host.authorize(binding, utterance)
    assert revoked_permit is not None
    await host.revoke(revoked_permit)
    await host.revoke(revoked_permit)
    await host.revoke(object())
    with pytest.raises(RealtimeTurnHostError, match="permit"):
        await host.submit(binding, utterance, revoked_permit)

    assert server.prompt_calls == []


@pytest.mark.asyncio
async def test_revoke_revalidates_under_history_lock_before_removing_permit() -> None:
    host, _server, session, _transport, _principal = _host()
    lock = _CountingLock()
    session["history_lock"] = lock
    permit = await host.authorize(_binding(), _utterance())
    assert permit is not None
    lock.entries = 0

    await host.revoke(permit)

    assert lock.entries == 1
    with pytest.raises(RealtimeTurnHostError, match="permit"):
        await host.submit(_binding(), _utterance(), permit)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("session_key", "rotated-session"),
        ("profile_home", "C:/profiles/work"),
        ("transport", object()),
        ("attachment.principal", object()),
        ("attachment.selection_generation", 8),
    ],
)
async def test_submit_revalidates_each_drift_dimension_and_consumes_permit(
    field: str, changed: object
) -> None:
    host, server, session, _transport, _principal = _host()
    binding = _binding()
    utterance = _utterance()
    permit = await host.authorize(binding, utterance)
    assert permit is not None
    original = _replace_drift_value(session, field, changed)

    with pytest.raises(RealtimeTurnHostError, match="permit"):
        await host.submit(binding, utterance, permit)
    _replace_drift_value(session, field, original)
    with pytest.raises(RealtimeTurnHostError, match="permit"):
        await host.submit(binding, utterance, permit)
    assert server.prompt_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("registry_change", ["removed", "replaced"])
async def test_submit_revalidates_runtime_record_identity_and_consumes_permit(
    registry_change: str,
) -> None:
    host, server, session, _transport, _principal = _host()
    binding = _binding()
    utterance = _utterance()
    permit = await host.authorize(binding, utterance)
    assert permit is not None
    if registry_change == "removed":
        del server._sessions["runtime-session"]
    else:
        server._sessions["runtime-session"] = dict(session)

    with pytest.raises(RealtimeTurnHostError, match="permit"):
        await host.submit(binding, utterance, permit)
    server._sessions["runtime-session"] = session
    with pytest.raises(RealtimeTurnHostError, match="permit"):
        await host.submit(binding, utterance, permit)
    assert server.prompt_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"result": None},
        {"result": {}},
        {"result": {"status": "steered"}},
        {"error": {"code": 4090, "message": "capacity"}},
        {"result": {"status": True}},
    ],
)
async def test_ambiguous_or_negative_prompt_response_fails_closed_and_consumes(
    response: object,
) -> None:
    host, server, _session, _transport, _principal = _host()
    server.prompt_response = response
    binding = _binding()
    utterance = _utterance()
    permit = await host.authorize(binding, utterance)
    assert permit is not None

    with pytest.raises(RealtimeTurnHostError, match="not accepted"):
        await host.submit(binding, utterance, permit)
    with pytest.raises(RealtimeTurnHostError, match="permit"):
        await host.submit(binding, utterance, permit)
    assert len(server.prompt_calls) == 1


@pytest.mark.asyncio
async def test_enqueue_exception_fails_closed_and_consumes_permit() -> None:
    host, server, _session, _transport, _principal = _host()
    server.prompt_exception = OSError("queue unavailable")
    binding = _binding()
    utterance = _utterance()
    permit = await host.authorize(binding, utterance)
    assert permit is not None

    with pytest.raises(RealtimeTurnHostError, match="enqueue failed"):
        await host.submit(binding, utterance, permit)
    server.prompt_exception = None
    with pytest.raises(RealtimeTurnHostError, match="permit"):
        await host.submit(binding, utterance, permit)
    assert len(server.prompt_calls) == 1


@pytest.mark.asyncio
async def test_positive_queued_response_returns_receipt_from_captured_handler() -> None:
    host, server, _session, _transport, _principal = _host()
    captured_handler = server._methods["prompt.submit"]
    server.prompt_response = {"result": {"status": "queued"}}
    server._methods["prompt.submit"] = lambda _rid, _params: {
        "result": {"status": "steered"}
    }
    permit = await host.authorize(_binding(), _utterance())
    assert permit is not None

    receipt = await host.submit(_binding(), _utterance(), permit)

    assert receipt.status == "queued"
    assert server.prompt_calls
    assert captured_handler is not server._methods["prompt.submit"]


@pytest.mark.asyncio
async def test_interrupt_ack_is_not_completion_and_wait_is_bounded() -> None:
    host, server, session, _transport, _principal = _host()
    session["running"] = True

    with pytest.raises(RealtimeTurnHostError, match="timed out"):
        await host.interrupt_and_wait(_binding(), timeout=0.01)

    assert server.interrupt_calls == [
        ("realtime-voice", {"session_id": "runtime-session"})
    ]
    assert session["running"] is True


@pytest.mark.asyncio
async def test_interrupt_waits_for_exact_session_to_stop_running() -> None:
    host, server, session, _transport, _principal = _host()
    session["running"] = True

    async def finish_turn() -> None:
        await asyncio.sleep(0.01)
        with session["history_lock"]:  # type: ignore[attr-defined]
            session["running"] = False

    finisher = asyncio.create_task(finish_turn())
    await host.interrupt_and_wait(_binding(), timeout=0.2)
    await finisher

    assert server.interrupt_calls == [
        ("realtime-voice", {"session_id": "runtime-session"})
    ]


@pytest.mark.asyncio
async def test_interrupt_rejects_error_ack_and_session_drift() -> None:
    host, server, session, _transport, _principal = _host()
    server.interrupt_response = {"error": {"code": 500, "message": "failed"}}
    with pytest.raises(RealtimeTurnHostError, match="interrupt was not accepted"):
        await host.interrupt_and_wait(_binding(), timeout=0.1)

    server.interrupt_response = {"result": {"status": "interrupted"}}
    session["profile_home"] = "C:/profiles/work"
    with pytest.raises(RealtimeTurnHostError, match="attachment"):
        await host.interrupt_and_wait(_binding(), timeout=0.1)
    assert len(server.interrupt_calls) == 1
