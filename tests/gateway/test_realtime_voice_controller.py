from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from agent.realtime_voice_admission import (
    AdmissionStatus,
    RealtimeSessionBinding,
    RealtimeUtterance,
)
from agent.realtime_voice_provider import (
    InputTranscript,
    RealtimeCapability,
    RealtimeToolResult,
    RealtimeVoiceEvent,
    RealtimeVoiceProvider,
    RealtimeVoiceSession,
    RealtimeVoiceSetup,
    SessionClosed,
    SessionFailure,
    SessionReady,
    ToolCall,
    TranscriptProvenance,
    TranscriptRole,
)
from agent.realtime_voice_registry import _reset_for_tests, register_provider
from gateway.realtime_voice_controller import (
    AudioFeedResult,
    ControllerLifecycle,
    GatewayRealtimeVoiceController,
    HostProjection,
    HostProjectionStatus,
)


_STOP = object()


class _Session(RealtimeVoiceSession):
    def __init__(
        self, capabilities: frozenset[RealtimeCapability] = frozenset()
    ) -> None:
        super().__init__(capabilities)
        self.incoming: asyncio.Queue[RealtimeVoiceEvent | object] = asyncio.Queue()
        self.audio: list[tuple[bytes, str | None]] = []
        self.close_calls = 0
        self.block_audio = False
        self.audio_started = asyncio.Event()
        self.audio_release = asyncio.Event()
        self.interrupt_calls = 0
        self.resume_calls: list[str] = []
        self.block_resume = False
        self.resume_started = asyncio.Event()
        self.resume_release = asyncio.Event()
        self.block_close = False
        self.audio_error: BaseException | None = None
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()

    async def send_audio(self, audio: bytes, *, mime_type: str | None = None) -> None:
        self.audio_started.set()
        if self.block_audio:
            await self.audio_release.wait()
        if self.audio_error is not None:
            raise self.audio_error
        self.audio.append((audio, mime_type))

    async def _submit_tool_results(
        self, batch_id: str, results: tuple[RealtimeToolResult, ...]
    ) -> None:
        raise AssertionError("controller must not dispatch tools")

    async def _interrupt(self) -> None:
        self.interrupt_calls += 1

    async def _resume_session(self, session_id: str) -> None:
        self.resume_calls.append(session_id)
        self.resume_started.set()
        if self.block_resume:
            await self.resume_release.wait()

    async def _events(self) -> AsyncIterator[RealtimeVoiceEvent]:
        while True:
            event = await self.incoming.get()
            if event is _STOP:
                return
            assert isinstance(event, RealtimeVoiceEvent)
            yield event

    async def _close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        if self.block_close:
            await self.close_release.wait()


class _Provider(RealtimeVoiceProvider):
    def __init__(self, session: _Session) -> None:
        self.session = session

    @property
    def name(self) -> str:
        return "fake"

    async def open_session(self, setup: RealtimeVoiceSetup) -> RealtimeVoiceSession:
        return self.session


class _ControlledProvider(_Provider):
    def __init__(self, session: _Session) -> None:
        super().__init__(session)
        self.open_started = asyncio.Event()
        self.open_release = asyncio.Event()

    async def open_session(self, setup: RealtimeVoiceSetup) -> RealtimeVoiceSession:
        self.open_started.set()
        await self.open_release.wait()
        return self.session


class _CancellationSuppressingProvider(_ControlledProvider):
    def __init__(self, session: _Session) -> None:
        super().__init__(session)
        self.suppressed_cancellation = False

    async def open_session(self, setup: RealtimeVoiceSetup) -> RealtimeVoiceSession:
        self.open_started.set()
        try:
            await self.open_release.wait()
        except asyncio.CancelledError:
            self.suppressed_cancellation = True
            return self.session
        return self.session


class _Host:
    def __init__(self) -> None:
        self.authorized: list[RealtimeUtterance] = []
        self.submitted: list[RealtimeUtterance] = []
        self.revoked: list[object] = []
        self.interrupt_entered = asyncio.Event()
        self.interrupt_release = asyncio.Event()
        self.interrupt_error: BaseException | None = None
        self.block_interrupt = False
        self._finalizations: set[object] = set()

    async def authorize(
        self, binding: RealtimeSessionBinding, utterance: RealtimeUtterance
    ) -> object | None:
        self.authorized.append(utterance)
        return object()

    async def revoke(self, permit: object) -> None:
        self.revoked.append(permit)

    async def submit(
        self,
        binding: RealtimeSessionBinding,
        utterance: RealtimeUtterance,
        permit: object,
    ) -> str:
        self.submitted.append(utterance)
        return "queued"

    async def interrupt_and_wait(
        self, binding: RealtimeSessionBinding, timeout: float
    ) -> None:
        self.interrupt_entered.set()
        if self.block_interrupt:
            await self.interrupt_release.wait()
        if self.interrupt_error is not None:
            raise self.interrupt_error

    def mint_finalization_for_test(self) -> object:
        receipt = object()
        self._finalizations.add(receipt)
        return receipt

    def validate_finalization(self, receipt: object) -> bool:
        return receipt in self._finalizations


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    _reset_for_tests()
    yield
    _reset_for_tests()


@pytest.fixture
def binding() -> RealtimeSessionBinding:
    return RealtimeSessionBinding(
        profile_id="profile",
        routing_key="route",
        runtime_session_id="runtime",
        durable_session_id="durable",
        provider_session_id="attachment",
        selection_generation=1,
    )


async def _eventually(predicate) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


@pytest.mark.asyncio
async def test_only_final_operator_transcript_is_admitted_once_and_tools_are_inert(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session()
    assert register_provider(_Provider(session))
    host = _Host()
    controller = GatewayRealtimeVoiceController(host)

    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await session.incoming.put(SessionReady(session_id="native"))
    partial = InputTranscript(
        item_id="item",
        turn_id="turn",
        text="hello",
        final=False,
        role=TranscriptRole.OPERATOR,
        provenance=TranscriptProvenance.OPERATOR_INPUT,
    )
    final = InputTranscript(
        item_id="item",
        turn_id="turn",
        text="hello",
        final=True,
        role=TranscriptRole.OPERATOR,
        provenance=TranscriptProvenance.OPERATOR_INPUT,
    )
    tool = ToolCall(
        call_id="call",
        batch_id="batch",
        turn_id="turn",
        response_id="response",
        name="danger",
        arguments={},
    )
    await session.incoming.put(partial)
    await session.incoming.put(tool)
    await session.incoming.put(final)
    await session.incoming.put(final)

    await _eventually(lambda: len(host.submitted) == 1)

    assert [utterance.text for utterance in host.authorized] == ["hello"]
    assert [utterance.text for utterance in host.submitted] == ["hello"]
    statuses = [event.lifecycle for event in controller.lifecycle_events]
    assert ControllerLifecycle.QUEUED in statuses
    assert ControllerLifecycle.THINKING not in statuses
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))
    await controller.project_host(HostProjection(binding, HostProjectionStatus.ACTING, detail="tool"))
    await controller.project_host(HostProjection(binding, HostProjectionStatus.SPEAKING, detail="answer"))
    await controller.project_host(HostProjection(
            binding, HostProjectionStatus.COMPLETED, detail="persisted",
            finalization=host.mint_finalization_for_test(),
        ))
    assert [event.lifecycle for event in controller.lifecycle_events][-3:] == [
        ControllerLifecycle.ACTING, ControllerLifecycle.SPEAKING, ControllerLifecycle.COMPLETED,
    ]
    assert controller.lifecycle_events[0].lifecycle is ControllerLifecycle.CONNECTING
    assert [event.sequence for event in controller.lifecycle_events] == list(
        range(1, len(controller.lifecycle_events) + 1)
    )
    assert all(event.binding == binding for event in controller.lifecycle_events)
    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_completed_projection_rejects_caller_manufactured_receipt(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session()
    assert register_provider(_Provider(session))
    host = _Host()
    controller = GatewayRealtimeVoiceController(host)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))

    with pytest.raises(RuntimeError, match="persistence receipt"):
        await controller.project_host(
            HostProjection(
                binding,
                HostProjectionStatus.COMPLETED,
                finalization=object(),
            )
        )

    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_audio_queue_copies_chunks_and_reports_overflow_without_stopping_events(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session()
    session.block_audio = True
    assert register_provider(_Provider(session))
    controller = GatewayRealtimeVoiceController(_Host(), audio_queue_size=1)
    await controller.open("fake", RealtimeVoiceSetup(), binding)

    source = bytearray(b"first")
    assert controller.feed_audio(source, mime_type="audio/pcm") is AudioFeedResult.ACCEPTED
    source[:] = b"xxxxx"
    await session.audio_started.wait()
    assert controller.feed_audio(b"second") is AudioFeedResult.ACCEPTED
    assert controller.feed_audio(b"third") is AudioFeedResult.OVERFLOW
    await session.incoming.put(SessionReady(session_id="native"))

    await _eventually(
        lambda: any(
            event.lifecycle is ControllerLifecycle.READY
            for event in controller.lifecycle_events
        )
    )
    assert not any(
        event.lifecycle is ControllerLifecycle.FAILED
        for event in controller.lifecycle_events
    )
    session.audio_release.set()
    await _eventually(lambda: len(session.audio) == 2)
    assert session.audio == [(b"first", "audio/pcm"), (b"second", None)]
    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_interrupt_holds_admission_until_release_and_failure_closes_attachment(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session(frozenset({RealtimeCapability.EXPLICIT_INTERRUPTION}))
    assert register_provider(_Provider(session))
    host = _Host()
    host.block_interrupt = True
    host.interrupt_error = TimeoutError("release timed out")
    controller = GatewayRealtimeVoiceController(host)
    await controller.open("fake", RealtimeVoiceSetup(), binding)

    interrupt = asyncio.create_task(controller.interrupt())
    await host.interrupt_entered.wait()
    assert session.interrupt_calls == 1
    final = InputTranscript(
        item_id="replacement",
        turn_id="turn-2",
        text="replacement",
        final=True,
        role=TranscriptRole.OPERATOR,
        provenance=TranscriptProvenance.OPERATOR_INPUT,
    )
    await session.incoming.put(final)
    await asyncio.sleep(0)
    assert host.authorized == []
    assert host.submitted == []

    host.interrupt_release.set()
    with pytest.raises(TimeoutError, match="release timed out"):
        await interrupt
    await _eventually(
        lambda: controller.lifecycle_events[-1].lifecycle
        is ControllerLifecycle.CLOSED
    )
    assert host.authorized == []
    assert host.submitted == []


@pytest.mark.asyncio
async def test_resume_retains_session_and_replay_ledger_and_fences_stale_generation(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session(frozenset({RealtimeCapability.SESSION_RESUMPTION}))
    assert register_provider(_Provider(session))
    host = _Host()
    controller = GatewayRealtimeVoiceController(host)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await session.incoming.put(SessionReady(session_id="resume-token"))
    original = InputTranscript(
        item_id="item",
        turn_id="turn",
        text="once",
        final=True,
        role=TranscriptRole.OPERATOR,
        provenance=TranscriptProvenance.OPERATOR_INPUT,
    )
    await session.incoming.put(original)
    await _eventually(lambda: len(host.submitted) == 1)
    session.block_audio = True
    assert controller.feed_audio(b"active-stale") is AudioFeedResult.ACCEPTED
    await session.audio_started.wait()
    assert controller.feed_audio(b"queued-stale") is AudioFeedResult.ACCEPTED
    await session.incoming.put(SessionFailure(code="network", message="lost"))
    await _eventually(
        lambda: controller.lifecycle_events[-1].lifecycle
        is ControllerLifecycle.RECONNECTING
    )

    assert session.audio == []
    assert controller._audio_queue.empty()
    assert controller.feed_audio(b"stale") is AudioFeedResult.RECONNECTING
    assert controller._audio_task is None
    session.block_audio = False
    await controller.resume()
    assert controller.feed_audio(b"fresh-audio") is AudioFeedResult.ACCEPTED
    await _eventually(lambda: session.audio == [(b"fresh-audio", None)])
    assert session.resume_calls == ["resume-token"]
    assert controller.lifecycle_events[-1].transport_generation == 2
    assert controller._audio_task is not None and not controller._audio_task.done()
    await controller._handle_event(  # prior transport callback arriving late
        InputTranscript(
            item_id="stale",
            turn_id="old-turn",
            text="stale",
            final=True,
            role=TranscriptRole.OPERATOR,
            provenance=TranscriptProvenance.OPERATOR_INPUT,
        ),
        1,
    )
    await session.incoming.put(original)
    fresh = InputTranscript(
        item_id="fresh",
        turn_id="new-turn",
        text="fresh",
        final=True,
        role=TranscriptRole.OPERATOR,
        provenance=TranscriptProvenance.OPERATOR_INPUT,
    )
    await session.incoming.put(fresh)
    await _eventually(lambda: len(host.submitted) == 2)
    assert [utterance.text for utterance in host.submitted] == ["once", "fresh"]
    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_close_wins_resume_race_without_resurrecting_workers(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session(frozenset({RealtimeCapability.SESSION_RESUMPTION}))
    assert register_provider(_Provider(session))
    controller = GatewayRealtimeVoiceController(_Host())
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await session.incoming.put(SessionReady(session_id="resume-token"))
    await session.incoming.put(SessionFailure(code="network", message="lost"))
    await _eventually(lambda: controller._reconnecting)

    session.block_resume = True
    resume = asyncio.create_task(controller.resume())
    await session.resume_started.wait()
    await controller.close(reason="close wins resume race")
    session.resume_release.set()
    await resume

    assert controller._closed is True
    assert controller.lifecycle_events[-1].lifecycle is ControllerLifecycle.CLOSED
    assert controller._event_task is None or controller._event_task.done()
    assert controller._audio_task is None or controller._audio_task.done()
    assert all(
        event.lifecycle is not ControllerLifecycle.CONNECTING
        for event in controller.lifecycle_events
        if event.sequence > next(
            item.sequence
            for item in controller.lifecycle_events
            if item.lifecycle is ControllerLifecycle.CLOSED
        )
    )


@pytest.mark.asyncio
async def test_concurrent_resume_has_single_owner_and_no_orphan_pumps(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session(frozenset({RealtimeCapability.SESSION_RESUMPTION}))
    assert register_provider(_Provider(session))
    controller = GatewayRealtimeVoiceController(_Host())
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await session.incoming.put(SessionReady(session_id="resume-token"))
    await session.incoming.put(SessionFailure(code="network", message="lost"))
    await _eventually(lambda: controller._reconnecting)

    session.block_resume = True
    first = asyncio.create_task(controller.resume())
    second = asyncio.create_task(controller.resume())
    await session.resume_started.wait()
    session.resume_release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert session.resume_calls == ["resume-token"]
    assert controller._transport_generation == 2
    assert sum(result is None for result in results) == 1
    assert sum(
        isinstance(result, RuntimeError)
        and str(result) == "session is not reconnecting"
        for result in results
    ) == 1
    assert controller._event_task is not None and not controller._event_task.done()
    assert controller._audio_task is not None and not controller._audio_task.done()

    await controller.close(reason="concurrent resume test")
    assert controller._event_task is None or controller._event_task.done()
    assert controller._audio_task is None or controller._audio_task.done()
    live_pumps = [
        task
        for task in asyncio.all_tasks()
        if not task.done()
        and task.get_coro().__qualname__.endswith(("._pump_events", "._pump_audio"))
    ]
    assert live_pumps == []


@pytest.mark.asyncio
async def test_cancelled_resume_owner_does_not_poison_prior_pump_or_retry(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session(frozenset({RealtimeCapability.SESSION_RESUMPTION}))
    assert register_provider(_Provider(session))
    controller = GatewayRealtimeVoiceController(_Host())
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await session.incoming.put(SessionReady(session_id="resume-token"))
    await session.incoming.put(SessionFailure(code="network", message="lost"))
    await _eventually(lambda: controller._reconnecting)

    prior_release = asyncio.Event()
    prior_pump = asyncio.create_task(prior_release.wait())
    controller._event_task = prior_pump  # retained pump still winding down
    owner = asyncio.create_task(controller.resume())
    await _eventually(lambda: controller._resume_lock.locked())
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner

    assert prior_pump.cancelled() is False
    assert controller._reconnecting is True
    assert session.resume_calls == []
    prior_release.set()
    await prior_pump
    await controller.resume()

    assert session.resume_calls == ["resume-token"]
    assert controller._transport_generation == 2
    await controller.close(reason="cancelled resume owner test")
    assert controller._event_task is None or controller._event_task.done()
    assert controller._audio_task is None or controller._audio_task.done()


@pytest.mark.parametrize(
    ("kwargs", "error_type"),
    [
        ({"replay_capacity": 0}, ValueError),
        ({"replay_capacity": True}, TypeError),
        ({"max_transcript_chars": 0}, ValueError),
        ({"max_transcript_chars": 1.5}, TypeError),
        ({"interrupt_timeout": 0}, ValueError),
        ({"interrupt_timeout": float("inf")}, ValueError),
        ({"interrupt_timeout": float("nan")}, ValueError),
        ({"interrupt_timeout": True}, TypeError),
    ],
)
def test_invalid_controller_bounds_fail_before_provider_open(
    kwargs: dict[str, object], error_type: type[Exception]
) -> None:
    with pytest.raises(error_type):
        GatewayRealtimeVoiceController(_Host(), **kwargs)


@pytest.mark.parametrize(
    ("timeout", "error_type"),
    [
        (0, ValueError),
        (float("inf"), ValueError),
        (float("nan"), ValueError),
        (True, TypeError),
    ],
)
@pytest.mark.asyncio
async def test_invalid_resume_timeout_fails_before_session_check(
    timeout: object, error_type: type[Exception]
) -> None:
    controller = GatewayRealtimeVoiceController(_Host())
    with pytest.raises(error_type):
        await controller.resume(timeout=timeout)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_provider_close_is_projected_and_terminal_old_ledger_closes_once(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session()
    assert register_provider(_Provider(session))
    controller = GatewayRealtimeVoiceController(_Host())
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    old_ledger = controller._admission
    assert old_ledger is not None
    native_close = SessionClosed(reason="native")
    await session.incoming.put(native_close)
    await _eventually(
        lambda: controller.lifecycle_events[-1].lifecycle
        is ControllerLifecycle.CLOSED
    )

    assert any(
        event.provider_event is native_close for event in controller.lifecycle_events
    )
    terminal_count = sum(
        event.lifecycle is ControllerLifecycle.CLOSED
        for event in controller.lifecycle_events
    )
    assert terminal_count == 1
    result = await old_ledger.admit(
        InputTranscript(
            item_id="late",
            turn_id="late-turn",
            text="late",
            final=True,
            role=TranscriptRole.OPERATOR,
            provenance=TranscriptProvenance.OPERATOR_INPUT,
        )
    )
    assert result.status is AdmissionStatus.CLOSED
    await controller.close(reason="again")
    assert session.close_calls == 1
    assert sum(
        event.lifecycle is ControllerLifecycle.CLOSED
        for event in controller.lifecycle_events
    ) == 1


@pytest.mark.asyncio
async def test_cancelled_close_waiter_does_not_orphan_cleanup_or_terminal_event(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session()
    session.block_close = True
    assert register_provider(_Provider(session))
    controller = GatewayRealtimeVoiceController(_Host())
    await controller.open("fake", RealtimeVoiceSetup(), binding)

    close_waiter = asyncio.create_task(controller.close(reason="cancel race"))
    await session.close_started.wait()
    close_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_waiter
    session.close_release.set()

    await _eventually(
        lambda: controller.lifecycle_events[-1].lifecycle
        is ControllerLifecycle.CLOSED
    )
    await controller.close(reason="again")
    assert session.close_calls == 1
    assert sum(
        event.lifecycle is ControllerLifecycle.CLOSED
        for event in controller.lifecycle_events
    ) == 1


@pytest.mark.asyncio
async def test_all_terminal_admission_statuses_are_visibly_projected(binding: RealtimeSessionBinding) -> None:
    session = _Session()
    assert register_provider(_Provider(session))
    controller = GatewayRealtimeVoiceController(_Host(), replay_capacity=1)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    participant = InputTranscript(item_id="participant", turn_id="participant-turn", text="no", final=True,
        role=TranscriptRole.PARTICIPANT, provenance=TranscriptProvenance.PARTICIPANT_INPUT_AUDIO)
    await session.incoming.put(participant)
    await session.incoming.put(participant)
    await session.incoming.put(InputTranscript(item_id="capacity", turn_id="capacity-turn", text="later", final=True,
        role=TranscriptRole.OPERATOR, provenance=TranscriptProvenance.OPERATOR_INPUT))
    await _eventually(lambda: sum(e.admission_status is not None for e in controller.lifecycle_events) == 3)
    assert controller._admission is not None
    await controller._admission.close()
    await controller._handle_event(InputTranscript(
        item_id="closed", turn_id="closed-turn", text="closed", final=True,
        role=TranscriptRole.OPERATOR, provenance=TranscriptProvenance.OPERATOR_INPUT,
    ), 1)
    assert [e.admission_status for e in controller.lifecycle_events if e.admission_status] == [
        AdmissionStatus.REJECTED, AdmissionStatus.DUPLICATE,
        AdmissionStatus.CAPACITY_EXHAUSTED, AdmissionStatus.CLOSED]
    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_audio_send_failure_is_visible_and_terminal(binding: RealtimeSessionBinding) -> None:
    session = _Session()
    session.audio_error = OSError("speaker transport failed")
    assert register_provider(_Provider(session))
    controller = GatewayRealtimeVoiceController(_Host())
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    assert controller.feed_audio(b"boom") is AudioFeedResult.ACCEPTED
    await _eventually(lambda: controller.lifecycle_events[-1].lifecycle is ControllerLifecycle.CLOSED)
    assert any(e.lifecycle is ControllerLifecycle.FAILED and "speaker transport failed" in e.detail for e in controller.lifecycle_events)
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_cancelled_open_closes_late_provider_session_before_propagating(binding: RealtimeSessionBinding) -> None:
    session = _Session()
    provider = _CancellationSuppressingProvider(session)
    assert register_provider(provider)
    controller = GatewayRealtimeVoiceController(_Host())
    opening = asyncio.create_task(controller.open("fake", RealtimeVoiceSetup(), binding))
    await provider.open_started.wait()
    opening.cancel()
    provider.open_release.set()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert session.close_calls == 1
    assert provider.suppressed_cancellation is False
    assert controller.lifecycle_events[-1].lifecycle is ControllerLifecycle.CLOSED


@pytest.mark.asyncio
async def test_external_close_during_open_cannot_orphan_late_session(binding: RealtimeSessionBinding) -> None:
    session = _Session()
    provider = _ControlledProvider(session)
    assert register_provider(provider)
    controller = GatewayRealtimeVoiceController(_Host())
    opening = asyncio.create_task(controller.open("fake", RealtimeVoiceSetup(), binding))
    await provider.open_started.wait()
    closing = asyncio.create_task(controller.close(reason="external"))
    await asyncio.sleep(0)
    provider.open_release.set()
    await closing
    await opening
    assert session.close_calls == 1
    assert controller.lifecycle_events[-1].lifecycle is ControllerLifecycle.CLOSED
