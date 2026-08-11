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
    Interruption,
    OutputAudio,
    RealtimeCapability,
    RealtimeOutputAudioFormat,
    RealtimeResponseRequest,
    RealtimeToolResult,
    RealtimeVoiceEvent,
    RealtimeVoiceProvider,
    RealtimeVoiceSession,
    RealtimeVoiceSetup,
    SessionClosed,
    SessionFailure,
    SessionReady,
    ResponseCompleted,
    ResponseStarted,
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
    NativePlaybackReceipt,
)

from dataclasses import dataclass
import hashlib


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
        self.response_requests: list[RealtimeResponseRequest] = []
        self.response_started = asyncio.Event()
        self.response_release = asyncio.Event()
        self.response_cancelled = asyncio.Event()
        self.block_response = False
        self.response_error: BaseException | None = None

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

    async def _start_response(self, request: RealtimeResponseRequest) -> None:
        self.response_started.set()
        try:
            if self.block_response:
                await self.response_release.wait()
        except asyncio.CancelledError:
            self.response_cancelled.set()
            raise
        if self.response_error is not None:
            raise self.response_error
        self.response_requests.append(request)

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
        current = asyncio.current_task()
        sends = [
            task for task in self._response_send_tasks.values()
            if task is not current
        ]
        for task in sends:
            task.cancel()
        if sends:
            await asyncio.gather(*sends, return_exceptions=True)


class _ResponseStartedBeforeReturnSession(_Session):
    def __init__(self, turn_marker: str) -> None:
        super().__init__(frozenset({RealtimeCapability.EXPLICIT_RESPONSE}))
        self.turn_marker = turn_marker
        self.block_response = True

    async def _start_response(self, request: RealtimeResponseRequest) -> None:
        self.response_started.set()
        await self.incoming.put(ResponseStarted("response-race", self.turn_marker))
        if self.block_response:
            await self.response_release.wait()
        if self.response_error is not None:
            raise self.response_error
        self.response_requests.append(request)


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
        self.claims: list[tuple[RealtimeSessionBinding, object]] = []
        self.claim_started = asyncio.Event()
        self.claim_release = asyncio.Event()
        self.block_claim = False
        self.claim_request: RealtimeResponseRequest | None = None
        self.close_attachment_calls = 0

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

    async def claim_native_response(
        self, binding: RealtimeSessionBinding, finalization: object
    ) -> RealtimeResponseRequest:
        self.claims.append((binding, finalization))
        self.claim_started.set()
        if self.block_claim:
            await self.claim_release.wait()
        assert self.claim_request is not None
        return self.claim_request

    async def close_attachment(self, binding: RealtimeSessionBinding) -> None:
        self.close_attachment_calls += 1


@dataclass(frozen=True)
class _Finalization:
    durable_session_id: str
    assistant_message_id: int
    turn_marker: str


class _Lease:
    def __init__(self, response_id: str, turn_marker: str, generation: int) -> None:
        self.lease_id = "lease"
        self.response_id = response_id
        self.turn_marker = turn_marker
        self.transport_generation = generation
        self.writes: list[bytes] = []
        self.write_started = asyncio.Event()
        self.write_release = asyncio.Event()
        self.block_write = False
        self.cancel_write = False
        self.write_cancelled = asyncio.Event()
        self.finish_started = asyncio.Event()
        self.finish_release = asyncio.Event()
        self.receipt: NativePlaybackReceipt | None = None
        self.finish_error: BaseException | None = None
        self.close_calls = 0
        self.close_error: BaseException | None = None
        self.close_observer = None

    async def write_pcm(self, data: bytes) -> None:
        self.write_started.set()
        try:
            if self.block_write:
                await self.write_release.wait()
            if self.cancel_write:
                raise asyncio.CancelledError
        except asyncio.CancelledError:
            self.write_cancelled.set()
            raise
        self.writes.append(bytes(data))

    async def finish_and_wait(self, timeout: float) -> NativePlaybackReceipt:
        self.finish_started.set()
        await self.finish_release.wait()
        if self.finish_error is not None:
            raise self.finish_error
        assert self.receipt is not None
        return self.receipt

    async def interrupt_and_wait(self, timeout: float) -> NativePlaybackReceipt:
        raise AssertionError("barge-in is out of scope")

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_observer is not None:
            self.close_observer()
        if self.close_error is not None:
            raise self.close_error


class _Sink:
    def __init__(self) -> None:
        self.lease: _Lease | None = None
        self.lease_to_return: _Lease | None = None
        self.opens: list[tuple[object, str, str, object, int]] = []
        self.open_started = asyncio.Event()
        self.open_release = asyncio.Event()
        self.block_open = False
        self.cancel_open = False
        self.open_cancelled = asyncio.Event()

    async def open_lease(self, binding, response_id, turn_marker, output_format, transport_generation):
        self.opens.append((binding, response_id, turn_marker, output_format, transport_generation))
        self.open_started.set()
        try:
            if self.block_open:
                await self.open_release.wait()
            if self.cancel_open:
                raise asyncio.CancelledError
        except asyncio.CancelledError:
            self.open_cancelled.set()
            raise
        self.lease = self.lease_to_return or _Lease(
            response_id, turn_marker, transport_generation
        )
        return self.lease

    def validate_playback_receipt(self, lease: _Lease, receipt: NativePlaybackReceipt) -> bool:
        return receipt is lease.receipt


def _request(binding: RealtimeSessionBinding, finalization: _Finalization) -> RealtimeResponseRequest:
    text = "canonical answer"
    return RealtimeResponseRequest(
        durable_session_id=binding.durable_session_id,
        assistant_message_id=finalization.assistant_message_id,
        turn_marker=finalization.turn_marker,
        canonical_text=text,
        content_digest=hashlib.sha256(text.encode()).hexdigest(),
        output_audio_format=RealtimeOutputAudioFormat(
            mime_type="audio/pcm", sample_rate_hz=24000, channels=1,
            sample_encoding="pcm_s16le", sample_width_bytes=2, endianness="little",
        ),
        allow_tools=False,
    )


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
async def test_native_output_claim_send_stream_and_physical_drain_are_exact(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session(frozenset({RealtimeCapability.EXPLICIT_RESPONSE}))
    assert register_provider(_Provider(session))
    host = _Host()
    finalization = _Finalization(binding.durable_session_id, 7, "turn-native")
    host._finalizations.add(finalization)
    host.claim_request = _request(binding, finalization)
    sink = _Sink()
    controller = GatewayRealtimeVoiceController(host, output_sink=sink)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))

    await controller.project_host(HostProjection(
        binding, HostProjectionStatus.COMPLETED, finalization=finalization
    ))
    assert host.claims == [(binding, finalization)]
    assert len(session.response_requests) == 1
    assert not hasattr(controller._native_output, "canonical_text")
    await session.incoming.put(ResponseStarted("response", "turn-native"))
    await _eventually(lambda: sink.lease is not None)
    lease = sink.lease
    assert lease is not None
    await session.incoming.put(OutputAudio(b"\x01\x02\x03\x04", "item", "turn-native", "response"))
    await _eventually(lambda: lease.writes == [b"\x01\x02\x03\x04"])
    await session.incoming.put(ResponseCompleted("response", "turn-native"))
    await lease.finish_started.wait()
    assert ControllerLifecycle.NATIVE_DRAINED not in [e.lifecycle for e in controller.lifecycle_events]
    lease.receipt = NativePlaybackReceipt("lease", "response", "turn-native", 1, 4, False)
    lease.finish_release.set()
    await _eventually(lambda: controller._native_output is None)
    assert [e.lifecycle for e in controller.lifecycle_events].count(ControllerLifecycle.NATIVE_DRAINED) == 1
    assert lease.close_calls == 1
    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_native_reservation_precedes_claim_and_survives_waiter_cancellation(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session(frozenset({RealtimeCapability.EXPLICIT_RESPONSE}))
    session.block_response = True
    assert register_provider(_Provider(session))
    host = _Host()
    finalization = _Finalization(binding.durable_session_id, 8, "turn-cancel")
    host._finalizations.add(finalization)
    host.claim_request = _request(binding, finalization)
    sink = _Sink()
    controller = GatewayRealtimeVoiceController(host, output_sink=sink)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))
    waiter = asyncio.create_task(controller.project_host(HostProjection(
        binding, HostProjectionStatus.COMPLETED, finalization=finalization
    )))
    await session.response_started.wait()
    assert controller._native_output is not None
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    with pytest.raises(RuntimeError, match="native output"):
        await controller.project_host(HostProjection(
            binding, HostProjectionStatus.COMPLETED, finalization=finalization
        ))
    session.response_release.set()
    await _eventually(lambda: len(session.response_requests) == 1)
    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_response_started_after_send_dispatch_opens_before_send_returns(
    binding: RealtimeSessionBinding,
) -> None:
    session = _ResponseStartedBeforeReturnSession("turn-wire-race")
    assert register_provider(_Provider(session))
    host = _Host()
    finalization = _Finalization(binding.durable_session_id, 80, "turn-wire-race")
    host._finalizations.add(finalization)
    host.claim_request = _request(binding, finalization)
    sink = _Sink()
    controller = GatewayRealtimeVoiceController(host, output_sink=sink)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))

    projection = asyncio.create_task(controller.project_host(HostProjection(
        binding, HostProjectionStatus.COMPLETED, finalization=finalization
    )))
    await session.response_started.wait()
    await _eventually(lambda: sink.lease is not None)
    state = controller._native_output
    assert state is not None
    assert state.send_dispatched is True
    assert state.send_accepted is False
    lease = sink.lease
    assert lease is not None
    await session.incoming.put(OutputAudio(
        b"\x01\x02", "item-race", "turn-wire-race", "response-race"
    ))
    await _eventually(lambda: lease.writes == [b"\x01\x02"])

    session.response_release.set()
    await projection
    assert state.send_accepted is True
    await controller.close(reason="test")


@pytest.mark.asyncio
async def test_response_started_before_send_dispatch_is_unsolicited(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session(frozenset({RealtimeCapability.EXPLICIT_RESPONSE}))
    assert register_provider(_Provider(session))
    host = _Host()
    host.block_claim = True
    finalization = _Finalization(binding.durable_session_id, 81, "turn-too-early")
    host._finalizations.add(finalization)
    host.claim_request = _request(binding, finalization)
    sink = _Sink()
    controller = GatewayRealtimeVoiceController(host, output_sink=sink)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))
    projection = asyncio.create_task(controller.project_host(HostProjection(
        binding, HostProjectionStatus.COMPLETED, finalization=finalization
    )))
    await host.claim_started.wait()
    state = controller._native_output
    assert state is not None
    assert state.send_dispatched is False

    await session.incoming.put(ResponseStarted("response-early", "turn-too-early"))
    await _eventually(lambda: controller._closed)
    assert sink.opens == []
    assert [event.lifecycle for event in controller.lifecycle_events].count(
        ControllerLifecycle.NATIVE_FAILED
    ) == 1
    await asyncio.gather(projection, return_exceptions=True)


@pytest.mark.asyncio
async def test_send_failure_after_response_started_closes_open_lease_once(
    binding: RealtimeSessionBinding,
) -> None:
    session = _ResponseStartedBeforeReturnSession("turn-late-send-fail")
    session.response_error = OSError("late provider failure")
    assert register_provider(_Provider(session))
    host = _Host()
    finalization = _Finalization(binding.durable_session_id, 82, "turn-late-send-fail")
    host._finalizations.add(finalization)
    host.claim_request = _request(binding, finalization)
    sink = _Sink()
    controller = GatewayRealtimeVoiceController(host, output_sink=sink)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))
    projection = asyncio.create_task(controller.project_host(HostProjection(
        binding, HostProjectionStatus.COMPLETED, finalization=finalization
    )))
    await _eventually(lambda: sink.lease is not None)
    lease = sink.lease
    assert lease is not None

    session.response_release.set()
    with pytest.raises(OSError, match="late provider failure"):
        await projection
    await _eventually(lambda: controller._closed)
    assert lease.close_calls == 1
    assert session.close_calls == 1
    assert [event.lifecycle for event in controller.lifecycle_events].count(
        ControllerLifecycle.NATIVE_FAILED
    ) == 1


@pytest.mark.asyncio
async def test_native_output_rejects_mismatches_and_unsolicited_interruption(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session(frozenset({RealtimeCapability.EXPLICIT_RESPONSE}))
    assert register_provider(_Provider(session))
    host = _Host()
    finalization = _Finalization(binding.durable_session_id, 9, "turn-exact")
    host._finalizations.add(finalization)
    host.claim_request = _request(binding, finalization)
    controller = GatewayRealtimeVoiceController(host, output_sink=_Sink())
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))
    await controller.project_host(HostProjection(
        binding, HostProjectionStatus.COMPLETED, finalization=finalization
    ))
    await controller._handle_event(ResponseStarted("response", "wrong-turn"), 1)
    await _eventually(lambda: controller._closed)
    assert ControllerLifecycle.FAILED in [e.lifecycle for e in controller.lifecycle_events]

    # A provider interruption is never authority in this pre-barge-in slice.
    session2 = _Session(frozenset({RealtimeCapability.EXPLICIT_RESPONSE}))
    _reset_for_tests()
    assert register_provider(_Provider(session2))
    controller2 = GatewayRealtimeVoiceController(_Host(), output_sink=_Sink())
    await controller2.open("fake", RealtimeVoiceSetup(), binding)
    await controller2._handle_event(Interruption("response", "turn"), 1)
    await _eventually(lambda: controller2._closed)


@pytest.mark.asyncio
async def test_native_pcm_chunk_must_align_to_claimed_sample_width(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session(frozenset({RealtimeCapability.EXPLICIT_RESPONSE}))
    assert register_provider(_Provider(session))
    host = _Host()
    finalization = _Finalization(binding.durable_session_id, 10, "turn-pcm32")
    host._finalizations.add(finalization)
    text = "canonical answer"
    host.claim_request = RealtimeResponseRequest(
        durable_session_id=binding.durable_session_id,
        assistant_message_id=10,
        turn_marker="turn-pcm32",
        canonical_text=text,
        content_digest=hashlib.sha256(text.encode()).hexdigest(),
        output_audio_format=RealtimeOutputAudioFormat(
            mime_type="audio/pcm", sample_rate_hz=24000, channels=1,
            sample_encoding="pcm_s32le", sample_width_bytes=4, endianness="little",
        ),
        allow_tools=False,
    )
    controller = GatewayRealtimeVoiceController(host, output_sink=_Sink())
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))
    await controller.project_host(HostProjection(
        binding, HostProjectionStatus.COMPLETED, finalization=finalization
    ))
    await controller._handle_event(ResponseStarted("response", "turn-pcm32"), 1)
    await controller._handle_event(
        OutputAudio(b"\x01\x02", "item", "turn-pcm32", "response"), 1
    )
    await _eventually(lambda: controller._closed)
    assert ControllerLifecycle.NATIVE_PLAYING not in [
        event.lifecycle for event in controller.lifecycle_events
    ]


@pytest.mark.asyncio
async def test_native_lease_rejects_equal_identifier_lookalike(
    binding: RealtimeSessionBinding,
) -> None:
    class _EqualLookalike:
        def __eq__(self, other: object) -> bool:
            return other == "response"

    session = _Session(frozenset({RealtimeCapability.EXPLICIT_RESPONSE}))
    assert register_provider(_Provider(session))
    host = _Host()
    finalization = _Finalization(binding.durable_session_id, 83, "turn-lease-type")
    host._finalizations.add(finalization)
    host.claim_request = _request(binding, finalization)
    sink = _Sink()
    lease = _Lease("response", "turn-lease-type", 1)
    lease.response_id = _EqualLookalike()  # type: ignore[assignment]
    sink.lease_to_return = lease
    controller = GatewayRealtimeVoiceController(host, output_sink=sink)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))
    await controller.project_host(HostProjection(
        binding, HostProjectionStatus.COMPLETED, finalization=finalization
    ))

    await controller._handle_event(ResponseStarted("response", "turn-lease-type"), 1)

    assert controller._closed
    assert lease.close_calls == 1
    assert ControllerLifecycle.NATIVE_STARTED not in [
        event.lifecycle for event in controller.lifecycle_events
    ]


@pytest.mark.asyncio
async def test_native_lease_property_failure_closes_acquired_lease(
    binding: RealtimeSessionBinding,
) -> None:
    class _PropertyFailureLease(_Lease):
        @property
        def response_id(self) -> str:
            raise RuntimeError("response identity unavailable")

        @response_id.setter
        def response_id(self, value: object) -> None:
            pass

    session = _Session(frozenset({RealtimeCapability.EXPLICIT_RESPONSE}))
    assert register_provider(_Provider(session))
    host = _Host()
    finalization = _Finalization(binding.durable_session_id, 84, "turn-property-fail")
    host._finalizations.add(finalization)
    host.claim_request = _request(binding, finalization)
    sink = _Sink()
    lease = _PropertyFailureLease("response", "turn-property-fail", 1)
    sink.lease_to_return = lease
    controller = GatewayRealtimeVoiceController(host, output_sink=sink)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))
    await controller.project_host(HostProjection(
        binding, HostProjectionStatus.COMPLETED, finalization=finalization
    ))

    await controller._handle_event(ResponseStarted("response", "turn-property-fail"), 1)

    assert controller._closed
    assert lease.close_calls == 1
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_native_send_failure_owner_is_not_cancelled_by_its_close_fence(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session(frozenset({RealtimeCapability.EXPLICIT_RESPONSE}))
    session.response_error = OSError("provider send failed")
    assert register_provider(_Provider(session))
    host = _Host()
    finalization = _Finalization(binding.durable_session_id, 11, "turn-send-fail")
    host._finalizations.add(finalization)
    host.claim_request = _request(binding, finalization)
    controller = GatewayRealtimeVoiceController(host, output_sink=_Sink())
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))
    original_fail = controller._fail_native
    fail_completed = asyncio.Event()
    fail_cancelled = asyncio.Event()

    async def tracked_fail(detail: str) -> None:
        try:
            await original_fail(detail)
        except asyncio.CancelledError:
            fail_cancelled.set()
            raise
        else:
            fail_completed.set()

    controller._fail_native = tracked_fail
    with pytest.raises(OSError, match="provider send failed"):
        await controller.project_host(HostProjection(
            binding, HostProjectionStatus.COMPLETED, finalization=finalization
        ))

    await fail_completed.wait()
    assert not fail_cancelled.is_set()
    assert controller._closed
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_native_drain_failure_owner_is_not_cancelled_by_its_close_fence(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session(frozenset({RealtimeCapability.EXPLICIT_RESPONSE}))
    assert register_provider(_Provider(session))
    host = _Host()
    finalization = _Finalization(binding.durable_session_id, 12, "turn-drain-fail")
    host._finalizations.add(finalization)
    host.claim_request = _request(binding, finalization)
    sink = _Sink()
    controller = GatewayRealtimeVoiceController(host, output_sink=sink)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))
    await controller.project_host(HostProjection(
        binding, HostProjectionStatus.COMPLETED, finalization=finalization
    ))
    await controller._handle_event(ResponseStarted("response", "turn-drain-fail"), 1)
    lease = sink.lease
    assert lease is not None
    await controller._handle_event(
        OutputAudio(b"\x01\x02", "item", "turn-drain-fail", "response"), 1
    )
    original_fail = controller._fail_native
    fail_completed = asyncio.Event()
    fail_cancelled = asyncio.Event()

    async def tracked_fail(detail: str) -> None:
        try:
            await original_fail(detail)
        except asyncio.CancelledError:
            fail_cancelled.set()
            raise
        else:
            fail_completed.set()

    controller._fail_native = tracked_fail
    lease.finish_error = OSError("physical drain failed")
    await controller._handle_event(ResponseCompleted("response", "turn-drain-fail"), 1)
    await lease.finish_started.wait()
    lease.finish_release.set()

    await fail_completed.wait()
    assert not fail_cancelled.is_set()
    assert controller._closed
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_external_close_cancels_blocked_native_lease_open_without_failure(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session(frozenset({RealtimeCapability.EXPLICIT_RESPONSE}))
    assert register_provider(_Provider(session))
    host = _Host()
    finalization = _Finalization(binding.durable_session_id, 85, "turn-open-close")
    host._finalizations.add(finalization)
    host.claim_request = _request(binding, finalization)
    sink = _Sink()
    sink.block_open = True
    controller = GatewayRealtimeVoiceController(host, output_sink=sink)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))
    await controller.project_host(HostProjection(
        binding, HostProjectionStatus.COMPLETED, finalization=finalization
    ))
    await session.incoming.put(ResponseStarted("response", "turn-open-close"))
    await sink.open_started.wait()

    await asyncio.wait_for(controller.close(reason="external open close"), timeout=1)

    assert sink.open_cancelled.is_set()
    assert controller._event_task is not None and controller._event_task.done()
    assert session.close_calls == 1
    assert ControllerLifecycle.NATIVE_FAILED not in [
        event.lifecycle for event in controller.lifecycle_events
    ]


@pytest.mark.asyncio
async def test_external_close_cancels_blocked_native_write_before_lease_close(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session(frozenset({RealtimeCapability.EXPLICIT_RESPONSE}))
    assert register_provider(_Provider(session))
    host = _Host()
    finalization = _Finalization(binding.durable_session_id, 86, "turn-write-close")
    host._finalizations.add(finalization)
    host.claim_request = _request(binding, finalization)
    sink = _Sink()
    controller = GatewayRealtimeVoiceController(host, output_sink=sink)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))
    await controller.project_host(HostProjection(
        binding, HostProjectionStatus.COMPLETED, finalization=finalization
    ))
    await session.incoming.put(ResponseStarted("response", "turn-write-close"))
    await _eventually(lambda: sink.lease is not None)
    lease = sink.lease
    assert lease is not None
    lease.block_write = True
    await session.incoming.put(OutputAudio(
        b"\x01\x02", "item", "turn-write-close", "response"
    ))
    await lease.write_started.wait()

    await asyncio.wait_for(controller.close(reason="external write close"), timeout=1)

    assert lease.write_cancelled.is_set()
    assert lease.close_calls == 1
    assert session.close_calls == 1
    assert ControllerLifecycle.NATIVE_FAILED not in [
        event.lifecycle for event in controller.lifecycle_events
    ]


@pytest.mark.asyncio
async def test_spontaneous_native_write_cancellation_fails_closed_once(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session(frozenset({RealtimeCapability.EXPLICIT_RESPONSE}))
    assert register_provider(_Provider(session))
    host = _Host()
    finalization = _Finalization(binding.durable_session_id, 87, "turn-write-cancel")
    host._finalizations.add(finalization)
    host.claim_request = _request(binding, finalization)
    sink = _Sink()
    controller = GatewayRealtimeVoiceController(host, output_sink=sink)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))
    await controller.project_host(HostProjection(
        binding, HostProjectionStatus.COMPLETED, finalization=finalization
    ))
    await session.incoming.put(ResponseStarted("response", "turn-write-cancel"))
    await _eventually(lambda: sink.lease is not None)
    lease = sink.lease
    assert lease is not None
    lease.cancel_write = True
    await session.incoming.put(OutputAudio(
        b"\x01\x02", "item", "turn-write-cancel", "response"
    ))

    await _eventually(lambda: controller._closed)
    assert lease.write_cancelled.is_set()
    assert lease.close_calls == 1
    assert session.close_calls == 1
    assert [event.lifecycle for event in controller.lifecycle_events].count(
        ControllerLifecycle.NATIVE_FAILED
    ) == 1


@pytest.mark.asyncio
async def test_spontaneous_native_lease_open_cancellation_fails_closed_once(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session(frozenset({RealtimeCapability.EXPLICIT_RESPONSE}))
    assert register_provider(_Provider(session))
    host = _Host()
    finalization = _Finalization(binding.durable_session_id, 88, "turn-open-cancel")
    host._finalizations.add(finalization)
    host.claim_request = _request(binding, finalization)
    sink = _Sink()
    sink.cancel_open = True
    controller = GatewayRealtimeVoiceController(host, output_sink=sink)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))
    await controller.project_host(HostProjection(
        binding, HostProjectionStatus.COMPLETED, finalization=finalization
    ))
    await session.incoming.put(ResponseStarted("response", "turn-open-cancel"))

    await _eventually(lambda: controller._closed)
    assert sink.open_cancelled.is_set()
    assert sink.lease is None
    assert session.close_calls == 1
    assert [event.lifecycle for event in controller.lifecycle_events].count(
        ControllerLifecycle.NATIVE_FAILED
    ) == 1


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
async def test_completed_projection_requires_exact_true_finalization_authority(
    binding: RealtimeSessionBinding,
) -> None:
    class _Truthy:
        def __bool__(self) -> bool:
            return True

    session = _Session(frozenset({RealtimeCapability.EXPLICIT_RESPONSE}))
    assert register_provider(_Provider(session))
    host = _Host()
    finalization = _Finalization(binding.durable_session_id, 70, "turn-truthy")
    host.validate_finalization = lambda receipt: _Truthy()  # type: ignore[method-assign]
    controller = GatewayRealtimeVoiceController(host, output_sink=_Sink())
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))

    with pytest.raises(RuntimeError, match="persistence receipt"):
        await controller.project_host(HostProjection(
            binding, HostProjectionStatus.COMPLETED, finalization=finalization
        ))

    assert host.claims == []
    assert controller._native_output is None
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
async def test_session_failure_with_unresolved_native_send_is_terminal_not_resumable(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session(frozenset({
        RealtimeCapability.EXPLICIT_RESPONSE,
        RealtimeCapability.SESSION_RESUMPTION,
    }))
    session.block_response = True
    assert register_provider(_Provider(session))
    host = _Host()
    finalization = _Finalization(binding.durable_session_id, 88, "turn-no-resume")
    host._finalizations.add(finalization)
    host.claim_request = _request(binding, finalization)
    sink = _Sink()
    controller = GatewayRealtimeVoiceController(host, output_sink=sink)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    await session.incoming.put(SessionReady(session_id="resume-token"))
    await _eventually(lambda: controller._resume_token == "resume-token")
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))
    projection = asyncio.create_task(controller.project_host(HostProjection(
        binding, HostProjectionStatus.COMPLETED, finalization=finalization
    )))
    await session.response_started.wait()

    await session.incoming.put(SessionFailure(code="network", message="lost native send"))
    await _eventually(lambda: controller._closed)

    assert controller._reconnecting is False
    assert controller._transport_generation == 1
    assert session.resume_calls == []
    assert session.response_cancelled.is_set()
    assert session.close_calls == 1
    assert sink.opens == []
    session.response_release.set()
    await asyncio.gather(projection, return_exceptions=True)
    await controller._handle_event(ResponseStarted("old-response", "turn-no-resume"), 1)
    assert sink.opens == []


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
async def test_native_lease_close_error_does_not_abort_terminal_cleanup(
    binding: RealtimeSessionBinding,
) -> None:
    session = _Session(frozenset({RealtimeCapability.EXPLICIT_RESPONSE}))
    assert register_provider(_Provider(session))
    host = _Host()
    finalization = _Finalization(binding.durable_session_id, 89, "turn-close-error")
    host._finalizations.add(finalization)
    host.claim_request = _request(binding, finalization)
    sink = _Sink()
    controller = GatewayRealtimeVoiceController(host, output_sink=sink)
    await controller.open("fake", RealtimeVoiceSetup(), binding)
    admission = controller._admission
    assert admission is not None
    await controller.project_host(HostProjection(binding, HostProjectionStatus.THINKING))
    await controller.project_host(HostProjection(
        binding, HostProjectionStatus.COMPLETED, finalization=finalization
    ))
    await controller._handle_event(ResponseStarted("response", "turn-close-error"), 1)
    lease = sink.lease
    state = controller._native_output
    assert lease is not None and state is not None
    observed: list[tuple[bool, bool]] = []
    lease.close_observer = lambda: observed.append((
        controller._native_output is state, state.lease_closed
    ))
    lease.close_error = OSError("speaker close failed")

    with pytest.raises(OSError, match="speaker close failed"):
        await controller.close(reason="close error")

    assert observed == [(True, True)]
    assert controller._native_output is None
    assert controller._closed is True
    assert controller.lifecycle_events[-1].lifecycle is ControllerLifecycle.CLOSED
    assert lease.close_calls == 1
    assert host.close_attachment_calls == 1
    assert session.close_calls == 1
    result = await admission.admit(InputTranscript(
        item_id="late-close-error", turn_id="late-close-error-turn", text="late",
        final=True, role=TranscriptRole.OPERATOR,
        provenance=TranscriptProvenance.OPERATOR_INPUT,
    ))
    assert result.status is AdmissionStatus.CLOSED

    await controller.close(reason="idempotent")
    assert lease.close_calls == 1
    assert session.close_calls == 1


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
