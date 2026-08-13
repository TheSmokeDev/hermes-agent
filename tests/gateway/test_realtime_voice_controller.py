from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.realtime_voice_controller as controller_module

from agent.realtime_voice_admission import (
    AdmissionStatus,
    RealtimeSessionBinding,
    RealtimeUtterance,
)
from agent.realtime_voice_provider import (
    InputSpeechStarted,
    InputTranscript,
    Interruption,
    OutputAudio,
    OutputTranscript,
    RealtimeOutputAudioFormat,
    RealtimeResponseRequest,
    RealtimeCapability,
    RealtimeToolResult,
    RealtimeVoiceEvent,
    RealtimeVoiceProvider,
    RealtimeVoiceSession,
    RealtimeVoiceSetup,
    ResponseCompleted,
    ResponseStarted,
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
_REQUIRED_NATIVE_CAPABILITIES = frozenset({
    RealtimeCapability.EXPLICIT_RESPONSE,
    RealtimeCapability.RESPONSE_CANCELLATION,
})


class _Session(RealtimeVoiceSession):
    def __init__(
        self,
        capabilities: frozenset[RealtimeCapability] = frozenset(),
        *,
        include_native_capabilities: bool = True,
    ) -> None:
        effective_capabilities = (
            capabilities | _REQUIRED_NATIVE_CAPABILITIES
            if include_native_capabilities
            else capabilities
        )
        super().__init__(effective_capabilities)
        self.response_requests: list[RealtimeResponseRequest] = []
        self.response_started = asyncio.Event()
        self.cancelled_responses: list[str] = []
        self.cancel_entered = asyncio.Event()
        self.cancel_release = asyncio.Event()
        self.cancel_release.set()
        self.block_cancel = False
        self.cancel_error: BaseException | None = None
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

    async def _start_response(self, request: RealtimeResponseRequest) -> None:
        self.response_requests.append(request)
        self.response_started.set()

    async def _cancel_response(self, response_id: str) -> None:
        self.cancelled_responses.append(response_id)
        self.cancel_entered.set()
        if self.block_cancel:
            await self.cancel_release.wait()
        if self.cancel_error is not None:
            raise self.cancel_error

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
        if getattr(self, "close_error", None) is not None:
            error = self.close_error
            self.close_error = None
            raise error


class _Provider(RealtimeVoiceProvider):
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.capabilities = session.capabilities
        self.open_calls = 0

    @property
    def name(self) -> str:
        return "fake"

    async def open_session(self, setup: RealtimeVoiceSetup) -> RealtimeVoiceSession:
        self.open_calls += 1
        return self.session


class _ControlledProvider(_Provider):
    def __init__(self, session: _Session) -> None:
        super().__init__(session)
        self.open_started = asyncio.Event()
        self.open_release = asyncio.Event()

    async def open_session(self, setup: RealtimeVoiceSetup) -> RealtimeVoiceSession:
        self.open_calls += 1
        self.open_started.set()
        await self.open_release.wait()
        return self.session


class _CancellationSuppressingProvider(_ControlledProvider):
    def __init__(self, session: _Session) -> None:
        super().__init__(session)
        self.suppressed_cancellation = False

    async def open_session(self, setup: RealtimeVoiceSetup) -> RealtimeVoiceSession:
        self.open_calls += 1
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
        self._native_finalization = object()
        self._native_requests: set[object] = set()
        self.native_leases: list[object] = []
        self.native_retired: list[tuple[object, object, object]] = []
        self.native_order: list[str] = []
        self.attachment_close_calls = 0
        self.native_closed = False

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
        return self._native_finalization

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

    async def reserve_native_output(self, binding, finalization):
        assert finalization is self._native_finalization
        text = "generic canonical answer"
        import hashlib

        request = _NativeRequest(
            _NativeReservation(
                binding.durable_session_id,
                len(self.submitted) + 1,
                f"host-turn-{len(self.submitted)}",
                hashlib.sha256(text.encode()).hexdigest(),
                RealtimeOutputAudioFormat(
                    "audio/pcm", 24000, 1, "pcm_s16le", 2, "little"
                ),
            ),
            text,
        )
        self._native_requests.add(request)
        return request

    def validate_native_output_request(self, request):
        if self.native_closed or request not in self._native_requests:
            raise ValueError("forged request")
        return request

    async def acquire_native_playback(
        self, request, *, lease_id, response_id, transport_generation
    ):
        self.validate_native_output_request(request)
        lease = _PlaybackLease(
            lease_id,
            response_id,
            request.reservation.turn_marker,
            transport_generation,
            self.native_order,
        )
        self.native_leases.append(lease)
        return lease

    def validate_native_playback_receipt(self, request, lease, receipt):
        self.validate_native_output_request(request)
        return receipt

    async def retire_native_output(self, request, lease, receipt):
        self.validate_native_output_request(request)
        self.native_retired.append((request, lease, receipt))
        self._native_requests.remove(request)
        return receipt

    async def close_attachment(self, binding) -> None:
        self.attachment_close_calls += 1
        self.native_closed = True
        self._native_requests.clear()


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
    session = _Session(
        frozenset({
            RealtimeCapability.EXPLICIT_RESPONSE,
            RealtimeCapability.RESPONSE_CANCELLATION,
        })
    )
    assert register_provider(_Provider(session))
    host = _NativeHost()
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
    await _eventually(lambda: len(session.response_requests) == 1)
    await _eventually(
        lambda: (
            ControllerLifecycle.THINKING
            in [item.lifecycle for item in controller.lifecycle_events]
        )
    )

    assert [utterance.text for utterance in host.authorized] == ["hello"]
    assert [utterance.text for utterance in host.submitted] == ["hello"]
    statuses = [event.lifecycle for event in controller.lifecycle_events]
    assert ControllerLifecycle.QUEUED in statuses
    assert ControllerLifecycle.THINKING in statuses
    assert (
        session.response_requests[0].canonical_text == "  persisted canonical answer  "
    )
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
    await controller.project_host(
        HostProjection(binding, HostProjectionStatus.THINKING)
    )

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
    assert (
        controller.feed_audio(source, mime_type="audio/pcm") is AudioFeedResult.ACCEPTED
    )
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
        lambda: controller.lifecycle_events[-1].lifecycle is ControllerLifecycle.CLOSED
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
    await _eventually(lambda: len(session.response_requests) == 1)
    await session.incoming.put(ResponseStarted("response-1", "host-turn-1"))
    await _eventually(lambda: len(host.native_leases) == 1)
    await session.incoming.put(
        OutputAudio(b"\x01\x00", "output-1", "host-turn-1", "response-1")
    )
    await session.incoming.put(ResponseCompleted("response-1", "host-turn-1"))
    await _eventually(lambda: len(host.native_retired) == 1)
    session.block_audio = True
    assert controller.feed_audio(b"active-stale") is AudioFeedResult.ACCEPTED
    await session.audio_started.wait()
    assert controller.feed_audio(b"queued-stale") is AudioFeedResult.ACCEPTED
    await session.incoming.put(SessionFailure(code="network", message="lost"))
    await _eventually(
        lambda: (
            controller.lifecycle_events[-1].lifecycle
            is ControllerLifecycle.RECONNECTING
        )
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
        if event.sequence
        > next(
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
    assert (
        sum(
            isinstance(result, RuntimeError)
            and str(result) == "session is not reconnecting"
            for result in results
        )
        == 1
    )
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
        lambda: controller.lifecycle_events[-1].lifecycle is ControllerLifecycle.CLOSED
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
    assert (
        sum(
            event.lifecycle is ControllerLifecycle.CLOSED
            for event in controller.lifecycle_events
        )
        == 1
    )


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
        lambda: controller.lifecycle_events[-1].lifecycle is ControllerLifecycle.CLOSED
    )
    await controller.close(reason="again")
    assert session.close_calls == 1
    assert (
        sum(
            event.lifecycle is ControllerLifecycle.CLOSED
            for event in controller.lifecycle_events
        )
        == 1
    )


@pytest.mark.asyncio
async def test_provider_close_failure_is_retryable_and_closed_is_truthful(binding):
    session = _Session()
    session.close_error = RuntimeError("secret provider close failure")
    assert register_provider(_Provider(session))
    controller = GatewayRealtimeVoiceController(_Host())
    await controller.open("fake", RealtimeVoiceSetup(), binding)

    with pytest.raises(RuntimeError, match="secret provider close failure"):
        await controller.close(reason="first")

    assert session.close_calls == 1
    assert controller._closed is False
    assert controller._closing is True
    assert not any(
        event.lifecycle is ControllerLifecycle.CLOSED
        for event in controller.lifecycle_events
    )
    assert controller.lifecycle_events[-1].lifecycle is ControllerLifecycle.FAILED
    assert controller.lifecycle_events[-1].detail == "provider cleanup failed"

    await controller.close(reason="retry")
    assert session.close_calls == 2
    assert controller._closed is True
    assert sum(
        event.lifecycle is ControllerLifecycle.CLOSED
        for event in controller.lifecycle_events
    ) == 1
    assert all("secret" not in event.detail for event in controller.lifecycle_events)


@pytest.mark.asyncio
async def test_cancelled_provider_close_is_retryable_and_closed_is_truthful(binding):
    session = _Session()
    session.close_error = asyncio.CancelledError()
    assert register_provider(_Provider(session))
    controller = GatewayRealtimeVoiceController(_Host())
    await controller.open("fake", RealtimeVoiceSetup(), binding)

    with pytest.raises(asyncio.CancelledError):
        await controller.close(reason="first")

    assert session.close_calls == 1
    assert controller._closed is False
    assert not any(
        event.lifecycle is ControllerLifecycle.CLOSED
        for event in controller.lifecycle_events
    )

    await controller.close(reason="retry")
    assert session.close_calls == 2
    assert controller._closed is True
    assert sum(
        event.lifecycle is ControllerLifecycle.CLOSED
        for event in controller.lifecycle_events
    ) == 1


@pytest.mark.asyncio
async def test_all_terminal_admission_statuses_are_visibly_projected(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session()
    assert register_provider(_Provider(session))
    controller = GatewayRealtimeVoiceController(_Host(), replay_capacity=1)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    participant = InputTranscript(
        item_id="participant",
        turn_id="participant-turn",
        text="no",
        final=True,
        role=TranscriptRole.PARTICIPANT,
        provenance=TranscriptProvenance.PARTICIPANT_INPUT_AUDIO,
    )
    await session.incoming.put(participant)
    await session.incoming.put(participant)
    await session.incoming.put(
        InputTranscript(
            item_id="capacity",
            turn_id="capacity-turn",
            text="later",
            final=True,
            role=TranscriptRole.OPERATOR,
            provenance=TranscriptProvenance.OPERATOR_INPUT,
        )
    )
    await _eventually(
        lambda: (
            sum(e.admission_status is not None for e in controller.lifecycle_events)
            == 3
        )
    )
    assert controller._admission is not None
    await controller._admission.close()
    await controller._handle_event(
        InputTranscript(
            item_id="closed",
            turn_id="closed-turn",
            text="closed",
            final=True,
            role=TranscriptRole.OPERATOR,
            provenance=TranscriptProvenance.OPERATOR_INPUT,
        ),
        1,
    )
    assert [
        e.admission_status for e in controller.lifecycle_events if e.admission_status
    ] == [
        AdmissionStatus.REJECTED,
        AdmissionStatus.DUPLICATE,
        AdmissionStatus.CAPACITY_EXHAUSTED,
        AdmissionStatus.CLOSED,
    ]
    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_audio_send_failure_is_visible_and_terminal(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session()
    session.audio_error = OSError("speaker transport failed")
    assert register_provider(_Provider(session))
    controller = GatewayRealtimeVoiceController(_Host())
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    assert controller.feed_audio(b"boom") is AudioFeedResult.ACCEPTED
    await _eventually(
        lambda: controller.lifecycle_events[-1].lifecycle is ControllerLifecycle.CLOSED
    )
    assert any(
        e.lifecycle is ControllerLifecycle.FAILED and e.detail == "audio send failed"
        for e in controller.lifecycle_events
    )
    assert all(
        "speaker transport failed" not in e.detail for e in controller.lifecycle_events
    )
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_cancelled_open_closes_late_provider_session_before_propagating(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session()
    provider = _CancellationSuppressingProvider(session)
    assert register_provider(provider)
    controller = GatewayRealtimeVoiceController(_Host())
    opening = asyncio.create_task(
        controller.open("fake", RealtimeVoiceSetup(), binding)
    )
    await provider.open_started.wait()
    opening.cancel()
    provider.open_release.set()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert session.close_calls == 1
    assert provider.suppressed_cancellation is False
    assert controller.lifecycle_events[-1].lifecycle is ControllerLifecycle.CLOSED


@pytest.mark.asyncio
async def test_external_close_during_open_cannot_orphan_late_session(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session()
    provider = _ControlledProvider(session)
    assert register_provider(provider)
    controller = GatewayRealtimeVoiceController(_Host())
    opening = asyncio.create_task(
        controller.open("fake", RealtimeVoiceSetup(), binding)
    )
    await provider.open_started.wait()
    closing = asyncio.create_task(controller.close(reason="external"))
    await asyncio.sleep(0)
    provider.open_release.set()
    await closing
    await opening
    assert session.close_calls == 1
    assert controller.lifecycle_events[-1].lifecycle is ControllerLifecycle.CLOSED


@dataclass(frozen=True, slots=True)
class _NativeReservation:
    durable_session_id: str
    assistant_message_id: int
    turn_marker: str
    content_digest: str
    output_audio_format: RealtimeOutputAudioFormat


@dataclass(frozen=True, slots=True)
class _NativeRequest:
    reservation: _NativeReservation
    canonical_text: str


@dataclass(frozen=True, slots=True)
class _PlaybackReceipt:
    lease_id: str
    response_id: str
    turn_marker: str
    transport_generation: int
    accepted_bytes: int
    accepted_frames: int
    interrupted: bool


class _PlaybackLease:
    def __init__(
        self,
        lease_id: str,
        response_id: str,
        turn_marker: str,
        generation: int,
        order: list[str],
    ) -> None:
        self.lease_id = lease_id
        self.response_id = response_id
        self.turn_marker = turn_marker
        self.generation = generation
        self.order = order
        self.writes: list[bytes] = []
        self.close_calls = 0
        self.write_entered = asyncio.Event()
        self.write_release = asyncio.Event()
        self.write_release.set()
        self.block_write = False
        self.write_error: BaseException | None = None
        self.finish_calls = 0
        self.finish_cancelled = asyncio.Event()
        self.interrupt_calls = 0
        self.interrupt_entered = asyncio.Event()
        self.interrupt_release = asyncio.Event()
        self.interrupt_release.set()
        self.block_lease_interrupt = False
        self.interrupt_error: BaseException | None = None
        self.finish_entered = asyncio.Event()
        self.finish_release = asyncio.Event()
        self.finish_release.set()

    async def write_pcm(self, data: bytes) -> None:
        self.write_entered.set()
        if self.block_write:
            await self.write_release.wait()
        if self.write_error is not None:
            raise self.write_error
        if len(data) % 2:
            raise ValueError("unaligned PCM")
        self.writes.append(data)

    async def finish_and_wait(self, timeout: float) -> _PlaybackReceipt:
        self.order.append("lease_finish")
        self.finish_calls += 1
        self.finish_entered.set()
        try:
            await self.finish_release.wait()
        except asyncio.CancelledError:
            self.finish_cancelled.set()
            raise
        return _PlaybackReceipt(
            self.lease_id,
            self.response_id,
            self.turn_marker,
            self.generation,
            sum(map(len, self.writes)),
            len(self.writes),
            False,
        )

    async def interrupt_and_wait(self, timeout: float) -> _PlaybackReceipt:
        self.order.append("lease_interrupt")
        self.interrupt_calls += 1
        self.interrupt_entered.set()
        if self.block_lease_interrupt:
            await self.interrupt_release.wait()
        if self.interrupt_error is not None:
            raise self.interrupt_error
        return _PlaybackReceipt(
            self.lease_id,
            self.response_id,
            self.turn_marker,
            self.generation,
            sum(map(len, self.writes)),
            len(self.writes),
            True,
        )

    async def close(self) -> None:
        self.order.append("lease_close")
        self.close_calls += 1


class _NativeHost(_Host):
    def __init__(self) -> None:
        super().__init__()
        self.order: list[str] = []
        self.requests: set[_NativeRequest] = set()
        self.leases: list[_PlaybackLease] = []
        self.retired: list[tuple[_NativeRequest, _PlaybackLease, _PlaybackReceipt]] = []
        self.finalization = object()
        self.acquire_entered = asyncio.Event()
        self.acquire_release = asyncio.Event()
        self.acquire_release.set()
        self.block_acquire = False
        self.acquire_error: BaseException | None = None
        self.close_calls = 0
        self.closed = False
        self.validation_calls = 0
        self.retire_calls = 0
        self.turn_markers: list[str] = []

    async def submit(self, binding, utterance, permit):
        self.submitted.append(utterance)
        return self.finalization

    async def reserve_native_output(self, binding, finalization):
        assert finalization is self.finalization
        self.order.append("reserve")
        text = "  persisted canonical answer  "
        import hashlib

        marker = self.turn_markers.pop(0) if self.turn_markers else "host-turn"
        reservation = _NativeReservation(
            binding.durable_session_id,
            42,
            marker,
            hashlib.sha256(text.encode()).hexdigest(),
            RealtimeOutputAudioFormat("audio/pcm", 24000, 1, "pcm_s16le", 2, "little"),
        )
        request = _NativeRequest(reservation, text)
        self.requests.add(request)
        return request

    def validate_native_output_request(self, request):
        if self.closed or request not in self.requests:
            raise ValueError("forged request")
        return request

    async def acquire_native_playback(
        self, request, *, lease_id, response_id, transport_generation
    ):
        self.validate_native_output_request(request)
        self.order.append("acquire")
        self.acquire_entered.set()
        if self.block_acquire:
            await self.acquire_release.wait()
        if self.acquire_error is not None:
            raise self.acquire_error
        lease = _PlaybackLease(
            lease_id,
            response_id,
            request.reservation.turn_marker,
            transport_generation,
            self.order,
        )
        self.leases.append(lease)
        return lease

    def validate_native_playback_receipt(self, request, lease, receipt):
        self.validation_calls += 1
        return self._assert_native_playback_receipt(request, lease, receipt)

    def _assert_native_playback_receipt(self, request, lease, receipt):
        self.validate_native_output_request(request)
        assert receipt.lease_id == lease.lease_id
        assert receipt.response_id == lease.response_id
        assert receipt.turn_marker == request.reservation.turn_marker
        assert receipt.transport_generation == lease.generation
        assert receipt.accepted_bytes == sum(map(len, lease.writes))
        return receipt

    async def retire_native_output(self, request, lease, receipt):
        self.retire_calls += 1
        self._assert_native_playback_receipt(request, lease, receipt)
        self.order.append("retire")
        self.retired.append((request, lease, receipt))
        self.requests.remove(request)
        return receipt

    async def close_attachment(self, binding) -> None:
        self.order.append("host_close")
        self.close_calls += 1
        self.closed = True
        self.requests.clear()


@pytest.mark.asyncio
async def test_native_response_uses_exact_canonical_text_streams_pcm_and_completes_after_drain(
    binding,
):
    capabilities = frozenset({
        RealtimeCapability.EXPLICIT_RESPONSE,
        RealtimeCapability.RESPONSE_CANCELLATION,
        RealtimeCapability.OUTPUT_TRANSCRIPTION,
    })
    session = _Session(capabilities)
    assert register_provider(_Provider(session))
    host = _NativeHost()
    controller = GatewayRealtimeVoiceController(host)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await session.incoming.put(
        InputTranscript(
            item_id="input",
            turn_id="input-turn",
            text="question",
            final=True,
            role=TranscriptRole.OPERATOR,
            provenance=TranscriptProvenance.OPERATOR_INPUT,
        )
    )
    await _eventually(lambda: len(session.response_requests) == 1)
    request = session.response_requests[0]
    assert request.canonical_text == "  persisted canonical answer  "
    assert request.allow_tools is False
    assert host.order == ["reserve"]
    await session.incoming.put(ResponseStarted("response", "host-turn"))
    await _eventually(lambda: len(host.leases) == 1)
    await session.incoming.put(
        OutputTranscript("item", "host-turn", "response", "provider substitute", True)
    )
    await session.incoming.put(
        OutputAudio(b"\x01\x00\x02\x00", "item", "host-turn", "response")
    )
    await session.incoming.put(ResponseCompleted("response", "host-turn"))
    await _eventually(lambda: len(host.retired) == 1)
    assert host.leases[0].writes == [b"\x01\x00\x02\x00"]
    assert host.order == ["reserve", "acquire", "lease_finish", "retire"]
    assert controller.lifecycle_events[-1].lifecycle is ControllerLifecycle.COMPLETED
    assert controller.lifecycle_events[-1].detail == "native playback drained"
    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_provider_completion_waits_for_local_drain_before_retirement(binding):
    session = _Session(
        frozenset({
            RealtimeCapability.EXPLICIT_RESPONSE,
            RealtimeCapability.RESPONSE_CANCELLATION,
        })
    )
    assert register_provider(_Provider(session))
    host = _NativeHost()
    controller = GatewayRealtimeVoiceController(host)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await session.incoming.put(
        InputTranscript(
            item_id="input",
            turn_id="input-turn",
            text="question",
            final=True,
            role=TranscriptRole.OPERATOR,
            provenance=TranscriptProvenance.OPERATOR_INPUT,
        )
    )
    await _eventually(lambda: len(session.response_requests) == 1)
    await session.incoming.put(ResponseStarted("response", "host-turn"))
    await _eventually(lambda: len(host.leases) == 1)
    lease = host.leases[0]
    await session.incoming.put(
        OutputAudio(b"\x01\x00", "item", "host-turn", "response")
    )
    lease.finish_release.clear()
    await session.incoming.put(ResponseCompleted("response", "host-turn"))
    await lease.finish_entered.wait()
    assert host.retired == []
    assert ControllerLifecycle.COMPLETED not in {
        item.lifecycle for item in controller.lifecycle_events
    }
    lease.finish_release.set()
    await _eventually(lambda: len(host.retired) == 1)
    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_speech_start_fences_native_response_without_blocking_event_pump(binding):
    session = _Session()
    session.block_cancel = True
    session.cancel_release.clear()
    assert register_provider(_Provider(session))
    host = _NativeHost()
    host.block_interrupt = True
    host.interrupt_release.clear()
    controller = GatewayRealtimeVoiceController(host)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await session.incoming.put(
        InputTranscript(
            item_id="input",
            turn_id="input-turn",
            text="question",
            final=True,
            role=TranscriptRole.OPERATOR,
            provenance=TranscriptProvenance.OPERATOR_INPUT,
        )
    )
    await session.response_started.wait()
    await session.incoming.put(ResponseStarted("response", "host-turn"))
    await _eventually(lambda: len(host.leases) == 1)
    lease = host.leases[0]
    lease.block_lease_interrupt = True
    lease.interrupt_release.clear()

    speech = InputSpeechStarted("speech-item", 123)
    pump_marker = ToolCall(
        call_id="call",
        batch_id="batch",
        turn_id="turn",
        response_id="other-response",
        name="inert",
        arguments={},
    )
    await session.incoming.put(speech)
    await session.incoming.put(speech)
    await session.incoming.put(pump_marker)

    await session.cancel_entered.wait()
    await host.interrupt_entered.wait()
    await _eventually(
        lambda: any(event.provider_event is pump_marker for event in controller.lifecycle_events)
    )
    assert controller._native_response is not None
    assert controller._native_response.interrupted is True
    assert session.cancelled_responses == ["response"]
    assert lease.interrupt_calls == 0

    session.cancel_release.set()
    await lease.interrupt_entered.wait()
    assert lease.interrupt_calls == 1
    host.interrupt_release.set()
    lease.interrupt_release.set()
    await _eventually(lambda: len(host.retired) == 1)
    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_speech_start_upgrades_blocked_drain_without_blocking_event_pump(binding):
    session = _Session()
    assert register_provider(_Provider(session))
    host = _NativeHost()
    controller = GatewayRealtimeVoiceController(host)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await session.incoming.put(
        InputTranscript(
            item_id="input",
            turn_id="input-turn",
            text="question",
            final=True,
            role=TranscriptRole.OPERATOR,
            provenance=TranscriptProvenance.OPERATOR_INPUT,
        )
    )
    await session.response_started.wait()
    await session.incoming.put(ResponseStarted("response", "host-turn"))
    await _eventually(lambda: len(host.leases) == 1)
    lease = host.leases[0]
    await session.incoming.put(
        OutputAudio(b"\x01\x00", "item", "host-turn", "response")
    )
    await _eventually(lambda: lease.writes == [b"\x01\x00"])
    lease.finish_release.clear()
    await session.incoming.put(ResponseCompleted("response", "host-turn"))
    await lease.finish_entered.wait()
    terminal_owner = controller._native_response.terminal_task

    marker = ToolCall(
        call_id="call",
        batch_id="batch",
        turn_id="turn",
        response_id="other-response",
        name="inert",
        arguments={},
    )
    await session.incoming.put(InputSpeechStarted("speech-item", 123))
    await session.incoming.put(marker)

    await lease.finish_cancelled.wait()
    await lease.interrupt_entered.wait()
    await _eventually(
        lambda: any(event.provider_event is marker for event in controller.lifecycle_events)
    )
    assert controller._barge_in_barrier.response.terminal_task is terminal_owner
    assert session.cancelled_responses == ["response"]
    assert lease.finish_calls == 1
    assert lease.interrupt_calls == 1
    assert ControllerLifecycle.COMPLETED not in {
        event.lifecycle for event in controller.lifecycle_events
    }
    await _eventually(lambda: len(host.retired) == 1)
    assert len(host.retired) == 1
    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_replacement_final_waits_for_three_exact_barge_in_gates(binding):
    session = _Session()
    assert register_provider(_Provider(session))
    host = _NativeHost()
    host.block_interrupt = True
    host.interrupt_release.clear()
    controller = GatewayRealtimeVoiceController(host)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    first = InputTranscript(
        "input", "input-turn", "question", True,
        TranscriptRole.OPERATOR, TranscriptProvenance.OPERATOR_INPUT,
    )
    await session.incoming.put(first)
    await session.response_started.wait()
    await session.incoming.put(ResponseStarted("response", "host-turn"))
    await _eventually(lambda: len(host.leases) == 1)
    lease = host.leases[0]
    lease.block_lease_interrupt = True
    lease.interrupt_release.clear()

    replacement = InputTranscript(
        "speech-item", "replacement-turn", "replacement", True,
        TranscriptRole.OPERATOR, TranscriptProvenance.OPERATOR_INPUT,
    )
    await session.incoming.put(InputSpeechStarted("speech-item", 123))
    await session.incoming.put(replacement)
    await session.cancel_entered.wait()
    await lease.interrupt_entered.wait()
    await host.interrupt_entered.wait()
    assert [item.text for item in host.submitted] == ["question"]

    lease.interrupt_release.set()
    host.interrupt_release.set()
    await _eventually(
        lambda: controller._barge_in_barrier.playback_terminal.is_set()
        and controller._barge_in_barrier.host_terminal.is_set()
    )
    assert len(host.retired) == 1
    assert [item.text for item in host.submitted] == ["question"]
    assert controller._barge_in_barrier is not None
    assert controller._barge_in_barrier.transcript is replacement

    await session.incoming.put(Interruption("response", "host-turn"))
    await _eventually(lambda: len(host.submitted) == 2)
    assert [item.text for item in host.submitted] == ["question", "replacement"]
    assert controller._barge_in_barrier is None
    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_interrupt_cancels_only_bound_response_and_retires_interrupted_lease(
    binding,
):
    session = _Session(
        frozenset({
            RealtimeCapability.EXPLICIT_RESPONSE,
            RealtimeCapability.RESPONSE_CANCELLATION,
            RealtimeCapability.EXPLICIT_INTERRUPTION,
        })
    )
    assert register_provider(_Provider(session))
    host = _NativeHost()
    controller = GatewayRealtimeVoiceController(host)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await session.incoming.put(
        InputTranscript(
            item_id="input",
            turn_id="input-turn",
            text="question",
            final=True,
            role=TranscriptRole.OPERATOR,
            provenance=TranscriptProvenance.OPERATOR_INPUT,
        )
    )
    await _eventually(lambda: len(session.response_requests) == 1)
    await session.incoming.put(ResponseStarted("response", "host-turn"))
    await _eventually(lambda: len(host.leases) == 1)
    await controller.interrupt()
    assert session.cancelled_responses == ["response"]
    assert session.interrupt_calls == 0
    assert len(host.retired) == 1
    assert host.retired[0][2].interrupted is True
    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_native_owner_rejects_external_speaking_projection(binding):
    session = _Session(
        frozenset({
            RealtimeCapability.EXPLICIT_RESPONSE,
            RealtimeCapability.RESPONSE_CANCELLATION,
        })
    )
    assert register_provider(_Provider(session))
    host = _NativeHost()
    controller = GatewayRealtimeVoiceController(host)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await session.incoming.put(
        InputTranscript(
            item_id="input",
            turn_id="input-turn",
            text="question",
            final=True,
            role=TranscriptRole.OPERATOR,
            provenance=TranscriptProvenance.OPERATOR_INPUT,
        )
    )
    await _eventually(lambda: len(session.response_requests) == 1)
    with pytest.raises(RuntimeError, match="native response owns lifecycle"):
        await controller.project_host(
            HostProjection(binding, HostProjectionStatus.SPEAKING)
        )
    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_close_terminalizes_native_lease_before_host_authority_closes(binding):
    session = _Session(
        frozenset({
            RealtimeCapability.EXPLICIT_RESPONSE,
            RealtimeCapability.RESPONSE_CANCELLATION,
        })
    )
    assert register_provider(_Provider(session))
    host = _NativeHost()
    controller = GatewayRealtimeVoiceController(host)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await session.incoming.put(
        InputTranscript(
            item_id="input",
            turn_id="input-turn",
            text="question",
            final=True,
            role=TranscriptRole.OPERATOR,
            provenance=TranscriptProvenance.OPERATOR_INPUT,
        )
    )
    await session.response_started.wait()
    await session.incoming.put(ResponseStarted("response", "host-turn"))
    await host.acquire_entered.wait()

    await controller.close(reason="operator close")

    assert host.order == [
        "reserve",
        "acquire",
        "lease_interrupt",
        "retire",
        "host_close",
    ]
    assert host.close_calls == 1
    assert len(host.retired) == 1
    assert host.retired[0][2].interrupted is True
    assert host.requests == set()


@pytest.mark.asyncio
async def test_incomplete_native_host_api_is_rejected_before_provider_open(binding):
    session = _Session()
    provider = _Provider(session)
    assert register_provider(provider)
    controller = GatewayRealtimeVoiceController(object())

    try:
        with pytest.raises(RuntimeError, match="native host API"):
            await controller.open("fake", RealtimeVoiceSetup(), binding)
    finally:
        await controller.close(reason="test cleanup")

    assert provider.open_calls == 0
    assert controller._admission is None
    assert controller._event_task is None
    assert controller._audio_task is None


@pytest.mark.asyncio
async def test_open_unions_mandatory_native_capabilities_without_mutating_caller(
    binding, monkeypatch
):
    session = _Session(frozenset({RealtimeCapability.OUTPUT_TRANSCRIPTION}))
    captured: list[frozenset[RealtimeCapability]] = []

    async def open_session(provider_name, setup, *, required_capabilities):
        captured.append(required_capabilities)
        return session

    monkeypatch.setattr(controller_module, "open_realtime_voice_session", open_session)
    caller_required = frozenset({RealtimeCapability.OUTPUT_TRANSCRIPTION})
    host = _NativeHost()
    controller = GatewayRealtimeVoiceController(host)
    await controller.open(
        "fake",
        RealtimeVoiceSetup(),
        binding,
        required_capabilities=caller_required,
    )

    assert caller_required == frozenset({RealtimeCapability.OUTPUT_TRANSCRIPTION})
    assert captured == [caller_required | _REQUIRED_NATIVE_CAPABILITIES]
    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_incompatible_returned_session_closes_session_and_host_before_pumps(
    binding, monkeypatch
):
    session = _Session(
        frozenset({RealtimeCapability.EXPLICIT_RESPONSE}),
        include_native_capabilities=False,
    )

    async def open_session(provider_name, setup, *, required_capabilities):
        return session

    monkeypatch.setattr(controller_module, "open_realtime_voice_session", open_session)
    host = _NativeHost()
    controller = GatewayRealtimeVoiceController(host)

    try:
        with pytest.raises(RuntimeError, match="native realtime session incompatible"):
            await controller.open("fake", RealtimeVoiceSetup(), binding)
    finally:
        await controller.close(reason="test cleanup")

    assert session.close_calls == 1
    assert host.close_calls == 1
    assert controller._admission is None
    assert controller._event_task is None
    assert controller._audio_task is None
    terminal = controller.lifecycle_events[-3:]
    assert [event.lifecycle for event in terminal] == [
        ControllerLifecycle.FAILED,
        ControllerLifecycle.CLOSING,
        ControllerLifecycle.CLOSED,
    ]
    assert [event.detail for event in terminal] == [
        "native realtime session incompatible",
        "native realtime session incompatible",
        "native realtime session incompatible",
    ]


async def _open_pending_native_response(binding):
    session = _Session()
    assert register_provider(_Provider(session))
    host = _NativeHost()
    controller = GatewayRealtimeVoiceController(host)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await session.incoming.put(
        InputTranscript(
            item_id="input",
            turn_id="input-turn",
            text="question",
            final=True,
            role=TranscriptRole.OPERATOR,
            provenance=TranscriptProvenance.OPERATOR_INPUT,
        )
    )
    await session.response_started.wait()
    return controller, session, host


@pytest.mark.asyncio
async def test_response_start_turn_must_match_authenticated_reservation_before_acquire(
    binding,
):
    controller, _session, host = await _open_pending_native_response(binding)

    try:
        with pytest.raises(RuntimeError, match="native response identity"):
            await controller._handle_event(
                ResponseStarted("response", "forged-turn"), 1
            )
    finally:
        await controller.close(reason="test cleanup")

    assert host.leases == []


@pytest.mark.asyncio
async def test_native_event_subclass_is_rejected_without_reading_hostile_fields(
    binding,
):
    controller, _session, host = await _open_pending_native_response(binding)
    armed = False

    class HostileResponseStarted(ResponseStarted):
        def __getattribute__(self, name):
            if armed and name in {
                "response_id",
                "turn_id",
                "continuation_of_batch_id",
            }:
                raise AssertionError("hostile event field read")
            return super().__getattribute__(name)

    event = HostileResponseStarted("response", "host-turn")
    armed = True
    try:
        with pytest.raises(RuntimeError, match="exact native event type"):
            await controller._handle_event(event, 1)
    finally:
        await controller.close(reason="test cleanup")

    assert host.leases == []


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("response_id", " response"),
        ("response_id", "r" * 257),
        ("continuation_of_batch_id", "batch"),
    ],
)
@pytest.mark.asyncio
async def test_mutated_response_start_is_revalidated_before_acquire(
    binding, field_name, invalid_value
):
    controller, _session, host = await _open_pending_native_response(binding)
    event = ResponseStarted("response", "host-turn")
    object.__setattr__(event, field_name, invalid_value)

    try:
        with pytest.raises(RuntimeError, match="invalid native response event"):
            await controller._handle_event(event, 1)
    finally:
        await controller.close(reason="test cleanup")

    assert host.leases == []


class _HostileString(str):
    def strip(self, *args, **kwargs):
        raise AssertionError("hostile string method called")

    def encode(self, *args, **kwargs):
        raise AssertionError("hostile string method called")


@pytest.mark.asyncio
async def test_mutated_response_id_subclass_is_rejected_before_hostile_methods(binding):
    controller, _session, host = await _open_pending_native_response(binding)
    event = ResponseStarted("response", "host-turn")
    object.__setattr__(event, "response_id", _HostileString("response"))

    try:
        with pytest.raises(RuntimeError, match="invalid native response event"):
            await controller._handle_event(event, 1)
    finally:
        await controller.close(reason="test cleanup")

    assert host.leases == []


@pytest.mark.parametrize(
    "event",
    [
        OutputAudio(b"pcm", "item", "host-turn", "response"),
        OutputTranscript("item", "host-turn", "response", "text", True),
        ResponseCompleted("response", "host-turn"),
    ],
)
@pytest.mark.asyncio
async def test_mutated_native_event_fields_are_revalidated_before_lease_methods(
    binding, event
):
    controller, _session, host = await _open_pending_native_response(binding)
    await controller._handle_event(ResponseStarted("response", "host-turn"), 1)
    lease = host.leases[0]
    if type(event) is OutputAudio:
        object.__setattr__(event, "data", bytearray(b"pcm"))
    elif type(event) is OutputTranscript:
        object.__setattr__(event, "final", 1)
    else:
        object.__setattr__(event, "continuation_of_batch_id", "batch")

    try:
        with pytest.raises(RuntimeError, match="invalid native response event"):
            await controller._handle_event(event, 1)
    finally:
        await controller.close(reason="test cleanup")

    assert lease.writes == []
    assert "lease_finish" not in host.order


@pytest.mark.parametrize(
    "first_event",
    [
        OutputAudio(b"\x01\x00", "item-a", "host-turn", "response"),
        OutputTranscript("item-a", "host-turn", "response", "final", True),
    ],
)
@pytest.mark.asyncio
async def test_native_output_item_binding_rejects_later_audio_or_transcript_item(
    binding, first_event
):
    controller, _session, host = await _open_pending_native_response(binding)
    await controller._handle_event(ResponseStarted("response", "host-turn"), 1)
    lease = host.leases[0]
    await controller._handle_event(first_event, 1)
    writes_before = list(lease.writes)

    wrong_event = (
        OutputTranscript("item-b", "host-turn", "response", "final", True)
        if type(first_event) is OutputAudio
        else OutputAudio(b"\x02\x00", "item-b", "host-turn", "response")
    )
    try:
        with pytest.raises(RuntimeError, match="native response item mismatch"):
            await controller._handle_event(wrong_event, 1)
    finally:
        await controller.close(reason="test cleanup")

    assert lease.writes == writes_before


@pytest.mark.parametrize(
    ("events", "error"),
    [
        ([ResponseCompleted("response", "host-turn")], "native response missing audio"),
        ([OutputAudio(b"", "item", "host-turn", "response")], "invalid native response event"),
    ],
)
@pytest.mark.asyncio
async def test_native_completion_requires_successful_nonempty_audio_write(
    binding, events, error
):
    controller, _session, host = await _open_pending_native_response(binding)
    await controller._handle_event(ResponseStarted("response", "host-turn"), 1)
    lease = host.leases[0]

    try:
        with pytest.raises(RuntimeError, match=error):
            for event in events:
                await controller._handle_event(event, 1)
    finally:
        await controller.close(reason="test cleanup")

    assert lease.writes == []
    assert lease.finish_calls == 0
    assert host.retire_calls == 1  # close interrupts; completion did not retire
    assert ControllerLifecycle.COMPLETED not in {
        event.lifecycle for event in controller.lifecycle_events
    }


@pytest.mark.asyncio
async def test_output_transcripts_are_bounded_and_not_retained_in_lifecycle(binding):
    controller, _session, host = await _open_pending_native_response(binding)
    await controller._handle_event(ResponseStarted("response", "host-turn"), 1)
    baseline = len(controller.lifecycle_events)
    for index in range(20):
        await controller._handle_event(
            OutputTranscript(
                "item", "host-turn", "response", f"provider-{index}", True
            ),
            1,
        )
    projected = controller.lifecycle_events[baseline:]
    assert len(projected) == 20
    assert all(event.provider_event is None for event in projected)
    assert max(map(len, (event.detail for event in projected))) <= 32

    hostile = OutputTranscript("item", "host-turn", "response", "ok", True)
    object.__setattr__(hostile, "text", _HostileString("x" * 5_000_000))
    try:
        with pytest.raises(RuntimeError, match="invalid native response event"):
            await controller._handle_event(hostile, 1)
    finally:
        await controller.close(reason="test cleanup")

    assert host.leases[0].writes == []


@pytest.mark.asyncio
async def test_interrupt_waits_for_blocked_acquire_then_terminalizes_exact_lease(
    binding,
):
    controller, session, host = await _open_pending_native_response(binding)
    host.block_acquire = True
    host.acquire_release.clear()
    await session.incoming.put(ResponseStarted("response", "host-turn"))
    await host.acquire_entered.wait()

    interrupting = asyncio.create_task(controller.interrupt())
    await session.cancel_entered.wait()
    assert not interrupting.done()
    host.acquire_release.set()
    await interrupting

    assert session.cancelled_responses == ["response"]
    assert len(host.leases) == 1
    assert host.leases[0].interrupt_calls == 1
    assert host.validation_calls == 1
    assert host.retire_calls == 1
    assert controller.lifecycle_events[-1].lifecycle is ControllerLifecycle.LISTENING
    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_interrupt_preempts_blocked_finish_and_retires_only_interrupt_receipt(
    binding,
):
    controller, session, host = await _open_pending_native_response(binding)
    await controller._handle_event(ResponseStarted("response", "host-turn"), 1)
    lease = host.leases[0]
    await controller._handle_event(
        OutputAudio(b"\x01\x00", "item", "host-turn", "response"), 1
    )
    lease.finish_release.clear()
    completing = asyncio.create_task(
        controller._handle_event(ResponseCompleted("response", "host-turn"), 1)
    )
    await lease.finish_entered.wait()

    interrupting = asyncio.create_task(controller.interrupt())
    interrupt_done, _pending = await asyncio.wait({interrupting}, timeout=0.5)
    completion_done, _pending = await asyncio.wait({completing}, timeout=0.5)
    if not interrupt_done or not completion_done:
        lease.finish_release.set()
        await completing
        await interrupting
    assert interrupt_done == {interrupting}
    assert completion_done == {completing}
    await interrupting
    await completing

    assert lease.finish_calls == 1
    assert lease.finish_cancelled.is_set()
    assert lease.interrupt_calls == 1
    assert host.validation_calls == 1
    assert host.retire_calls == 1
    assert host.retired[0][2].interrupted is True
    assert not lease.finish_release.is_set()
    assert controller.lifecycle_events[-1].lifecycle is ControllerLifecycle.LISTENING
    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_interrupt_and_close_share_one_blocked_cancel_and_one_terminal_owner(
    binding,
):
    controller, session, host = await _open_pending_native_response(binding)
    await controller._handle_event(ResponseStarted("response", "host-turn"), 1)
    lease = host.leases[0]
    session.block_cancel = True
    session.cancel_release.clear()

    interrupting = asyncio.create_task(controller.interrupt())
    await session.cancel_entered.wait()
    closing = asyncio.create_task(controller.close(reason="concurrent close"))
    assert not closing.done()
    session.cancel_release.set()
    await asyncio.gather(interrupting, closing)

    assert session.cancelled_responses == ["response"]
    assert lease.interrupt_calls == 1
    assert host.validation_calls == 1
    assert host.retire_calls == 1
    assert host.close_calls == 1
    assert controller.lifecycle_events[-1].lifecycle is ControllerLifecycle.CLOSED


@pytest.mark.asyncio
async def test_native_cleanup_failure_forces_lease_and_host_close_with_safe_details(
    binding,
):
    controller, _session, host = await _open_pending_native_response(binding)
    await controller._handle_event(ResponseStarted("response", "host-turn"), 1)
    lease = host.leases[0]
    lease.interrupt_error = RuntimeError("secret raw provider failure")

    await controller.close(reason="operator close")

    assert lease.interrupt_calls == 1
    assert lease.close_calls == 1
    assert host.close_calls == 1
    assert host.requests == set()
    terminal = [
        event
        for event in controller.lifecycle_events
        if event.lifecycle
        in {
            ControllerLifecycle.FAILED,
            ControllerLifecycle.CLOSING,
            ControllerLifecycle.CLOSED,
        }
    ][-3:]
    assert [event.lifecycle for event in terminal] == [
        ControllerLifecycle.FAILED,
        ControllerLifecycle.CLOSING,
        ControllerLifecycle.CLOSED,
    ]
    assert {event.detail for event in terminal} == {"native response cleanup failed"}
    assert all("secret" not in event.detail for event in controller.lifecycle_events)


@pytest.mark.asyncio
async def test_session_closed_terminalizes_active_native_ledger_before_closed(binding):
    controller, session, host = await _open_pending_native_response(binding)
    await controller._handle_event(ResponseStarted("response", "host-turn"), 1)

    await session.incoming.put(SessionClosed(reason="provider closed"))
    await session.close_started.wait()
    await controller.close(reason="join observed close")

    assert host.retire_calls == 1
    assert host.requests == set()
    assert host.order.index("retire") < host.order.index("host_close")
    assert controller.lifecycle_events[-1].lifecycle is ControllerLifecycle.CLOSED


@pytest.mark.asyncio
async def test_host_cleanup_failure_is_sanitized_and_closed_means_lifecycle_ended(binding):
    class FailingCloseHost(_NativeHost):
        async def close_attachment(self, binding) -> None:
            await super().close_attachment(binding)
            raise RuntimeError("secret native close detail")

    session = _Session()
    assert register_provider(_Provider(session))
    host = FailingCloseHost()
    controller = GatewayRealtimeVoiceController(host)
    await controller.open("fake", RealtimeVoiceSetup(), binding)

    with pytest.raises(RuntimeError, match="secret native close detail"):
        await controller.close(reason="test")

    terminal = controller.lifecycle_events[-3:]
    assert [event.lifecycle for event in terminal] == [
        ControllerLifecycle.FAILED,
        ControllerLifecycle.CLOSING,
        ControllerLifecycle.CLOSED,
    ]
    assert terminal[0].detail == "native cleanup failed"
    assert {event.detail for event in terminal[1:]} == {"closed after cleanup failure"}
    assert all("secret" not in event.detail for event in controller.lifecycle_events)
    assert controller._closed is True
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_terminal_response_tombstones_are_bounded_and_reject_late_duplicate(
    binding,
):
    session = _Session()
    assert register_provider(_Provider(session))
    host = _NativeHost()
    host.turn_markers = [f"host-turn-{index}" for index in range(3)]
    controller = GatewayRealtimeVoiceController(host, replay_capacity=2)
    await controller.open("fake", RealtimeVoiceSetup(), binding)

    for index in range(3):
        transcript = InputTranscript(
            item_id=f"input-{index}",
            turn_id=f"input-turn-{index}",
            text=f"question-{index}",
            final=True,
            role=TranscriptRole.OPERATOR,
            provenance=TranscriptProvenance.OPERATOR_INPUT,
        )
        await controller._start_native_response(host.finalization, transcript, 1, index)
        assert len(session.response_requests) == index + 1
        await controller._handle_event(
            ResponseStarted(f"response-{index}", f"host-turn-{index}"), 1
        )
        await controller._handle_event(
            OutputAudio(
                b"\x01\x00",
                f"output-{index}",
                f"host-turn-{index}",
                f"response-{index}",
            ),
            1,
        )
        await controller._handle_event(
            ResponseCompleted(f"response-{index}", f"host-turn-{index}"), 1
        )

    assert len(controller._native_tombstone_ids) == 2
    assert list(controller._native_tombstone_order) == [
        ("response-1", "host-turn-1"),
        ("response-2", "host-turn-2"),
    ]
    with pytest.raises(RuntimeError, match="late duplicate native response event"):
        await controller._handle_event(
            ResponseCompleted("response-2", "host-turn-2"), 1
        )
    await controller.close(reason="test")


class _RealLeaseHost(_NativeHost):
    def __init__(self, mixer) -> None:
        super().__init__()
        self.mixer = mixer

    async def acquire_native_playback(
        self, request, *, lease_id, response_id, transport_generation
    ):
        self.validate_native_output_request(request)
        self.order.append("acquire")
        lease = self.mixer.acquire_native_playback(
            lease_id,
            response_id,
            request.reservation.turn_marker,
            transport_generation,
        )
        self.leases.append(lease)
        self.acquire_entered.set()
        return lease

    def validate_native_playback_receipt(self, request, lease, receipt):
        self.validation_calls += 1
        self.validate_native_output_request(request)
        validated = self.mixer.validate_native_receipt(receipt, lease)
        assert validated is receipt
        return receipt

    async def retire_native_output(self, request, lease, receipt):
        self.retire_calls += 1
        self.validate_native_output_request(request)
        self.order.append("retire")
        self.retired.append((request, lease, receipt))
        self.requests.remove(request)
        return receipt

    async def close_attachment(self, binding) -> None:
        for lease in self.leases:
            await lease.close()
        await super().close_attachment(binding)


async def _next_loop_turn() -> None:
    barrier = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(barrier.set_result, None)
    await barrier


@pytest.mark.asyncio
async def test_real_native_pcm_capacity_blocked_write_interrupts_and_stays_listening(
    binding,
):
    from plugins.platforms.discord.voice_mixer import VoiceMixer

    session = _Session()
    assert register_provider(_Provider(session))
    host = _RealLeaseHost(VoiceMixer(native_frame_capacity=1))
    controller = GatewayRealtimeVoiceController(host)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await session.incoming.put(
        InputTranscript(
            item_id="input",
            turn_id="input-turn",
            text="question",
            final=True,
            role=TranscriptRole.OPERATOR,
            provenance=TranscriptProvenance.OPERATOR_INPUT,
        )
    )
    await session.response_started.wait()
    await controller._handle_event(ResponseStarted("response", "host-turn"), 1)
    lease = host.leases[0]
    payload = struct.pack("<960h", *range(960))
    writing = asyncio.create_task(
        controller._handle_event(
            OutputAudio(payload, "item", "host-turn", "response"), 1
        )
    )
    for _ in range(20):
        if lease._space_waiters:
            break
        await _next_loop_turn()
    assert lease._space_waiters

    await controller.interrupt()
    await writing

    assert host.retire_calls == 1
    assert host.retired[0][2].interrupted is True
    assert controller.lifecycle_events[-1].lifecycle is ControllerLifecycle.LISTENING
    assert not any(
        event.lifecycle is ControllerLifecycle.FAILED
        for event in controller.lifecycle_events
    )
    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_timed_out_real_host_acquire_closes_late_lease_without_orphan():
    from gateway.realtime_voice_messaging_host import (
        GatewayRealtimeVoiceMessagingHost,
        RealtimeVoiceFinalizationReceipt,
    )
    from tests.gateway.test_realtime_voice_messaging_host import (
        _binding as messaging_binding,
        _host_fixture,
        _native_output_format,
        build_session_key,
    )
    from gateway.config import Platform

    runner, source, _entry, captured, capture = _host_fixture()
    await capture()
    marker = "host-turn"
    runner._session_db = SimpleNamespace(
        _db=MagicMock(
            get_messages=MagicMock(
                return_value=[
                    {
                        "id": 1,
                        "role": "user",
                        "content": "voice",
                        "display_metadata": {"realtime_voice_turn_marker": marker},
                    },
                    {
                        "id": 2,
                        "role": "assistant",
                        "content": "answer",
                        "tool_calls": [],
                    },
                ]
            )
        )
    )
    adapter = runner.adapters[Platform.DISCORD]
    adapter._voice_clients[111] = SimpleNamespace(is_connected=lambda: True)
    adapter._voice_connection_generations[111] = 4
    adapter._voice_mixer_generations[111] = 7
    host = GatewayRealtimeVoiceMessagingHost(
        captured[0], runner, output_audio_format=_native_output_format()
    )
    receipt = RealtimeVoiceFinalizationReceipt("durable-1", marker, 1, 2)
    host._finalizations.add(receipt)
    binding = messaging_binding(build_session_key(source))
    session = _Session()
    assert register_provider(_Provider(session))
    controller = GatewayRealtimeVoiceController(host, interrupt_timeout=0.01)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await controller._start_native_response(
        receipt,
        InputTranscript(
            item_id="input",
            turn_id="input-turn",
            text="question",
            final=True,
            role=TranscriptRole.OPERATOR,
            provenance=TranscriptProvenance.OPERATOR_INPUT,
        ),
        1,
        0,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    lease_closed = asyncio.Event()
    lease = SimpleNamespace(close=AsyncMock(side_effect=lease_closed.set))

    async def acquire(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return lease

    adapter.acquire_native_playback_lease = acquire
    starting = asyncio.create_task(
        controller._handle_event(ResponseStarted("response", marker), 1)
    )
    await entered.wait()

    state = controller._native_response
    assert state is not None and state.acquire_task is not None
    state.acquire_task.cancel()
    with pytest.raises(RuntimeError, match="native response cleanup failed"):
        interrupting = asyncio.create_task(controller.interrupt())
        while not host._closed:
            await _next_loop_turn()
        assert not interrupting.done()
        assert not any(
            event.lifecycle is ControllerLifecycle.CLOSED
            for event in controller.lifecycle_events
        )
        release.set()
        await interrupting
    await starting
    assert controller.lifecycle_events[-1].lifecycle is ControllerLifecycle.CLOSED
    assert host._closed is True
    await lease_closed.wait()
    for _ in range(20):
        if not host._acquisitions:
            break
        await _next_loop_turn()

    lease.close.assert_awaited_once()
    assert host._acquisitions == {}
