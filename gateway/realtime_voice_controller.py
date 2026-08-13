"""Provider-neutral lifecycle owner for one gateway realtime voice attachment."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from agent.realtime_voice_admission import (
    AdmissionStatus,
    FinalTranscriptAdmission,
    RealtimeSessionBinding,
)
from agent.realtime_voice_orchestrator import open_realtime_voice_session
from agent.realtime_voice_provider import (
    InputSpeechStarted,
    InputTranscript,
    OutputAudio,
    OutputTranscript,
    RealtimeCapability,
    RealtimeResponseRequest,
    RealtimeVoiceEvent,
    RealtimeVoiceSession,
    RealtimeVoiceSetup,
    ResponseCompleted,
    ResponseStarted,
    SessionClosed,
    SessionFailure,
    SessionReady,
    TranscriptProvenance,
    TranscriptRole,
)


class ControllerLifecycle(StrEnum):
    CONNECTING = "connecting"
    READY = "ready"
    LISTENING = "listening"
    QUEUED = "queued"
    THINKING = "thinking"
    ACTING = "acting"
    SPEAKING = "speaking"
    COMPLETED = "completed"
    RECONNECTING = "reconnecting"
    FAILED = "failed"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class ControllerEvent:
    sequence: int
    lifecycle: ControllerLifecycle
    binding: RealtimeSessionBinding
    transport_generation: int
    detail: str = ""
    provider_event: RealtimeVoiceEvent | None = None
    admission_status: AdmissionStatus | None = None


class HostProjectionStatus(StrEnum):
    THINKING = "thinking"
    ACTING = "acting"
    SPEAKING = "speaking"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class HostProjection:
    binding: RealtimeSessionBinding
    status: HostProjectionStatus
    detail: str = ""
    finalization: object | None = None


class AudioFeedResult(StrEnum):
    ACCEPTED = "accepted"
    UNAUTHORIZED = "unauthorized"
    OVERFLOW = "overflow"
    CLOSED = "closed"
    RECONNECTING = "reconnecting"


class RealtimeControllerHost(Protocol):
    async def authorize(
        self, binding: RealtimeSessionBinding, utterance: Any
    ) -> Any: ...

    async def revoke(self, permit: Any) -> None: ...

    async def submit(
        self, binding: RealtimeSessionBinding, utterance: Any, permit: Any
    ) -> Any: ...

    async def interrupt_and_wait(
        self, binding: RealtimeSessionBinding, timeout: float
    ) -> None: ...

    def validate_finalization(self, receipt: object) -> bool: ...

    async def reserve_native_output(
        self, binding: RealtimeSessionBinding, finalization: object
    ) -> object: ...

    def validate_native_output_request(self, request: object) -> object: ...

    async def acquire_native_playback(
        self,
        request: object,
        *,
        lease_id: str,
        response_id: str,
        transport_generation: int,
    ) -> object: ...

    def validate_native_playback_receipt(
        self, request: object, lease: object, receipt: object
    ) -> object: ...

    async def retire_native_output(
        self, request: object, lease: object, receipt: object
    ) -> object: ...

    async def close_attachment(
        self, binding: RealtimeSessionBinding | None
    ) -> None: ...


_REQUIRED_NATIVE_CAPABILITIES = frozenset({
    RealtimeCapability.EXPLICIT_RESPONSE,
    RealtimeCapability.RESPONSE_CANCELLATION,
})
_REQUIRED_NATIVE_HOST_METHODS = (
    "reserve_native_output",
    "validate_native_output_request",
    "acquire_native_playback",
    "validate_native_playback_receipt",
    "retire_native_output",
    "close_attachment",
)
_NATIVE_EVENT_TYPES = (
    ResponseStarted,
    OutputAudio,
    OutputTranscript,
    ResponseCompleted,
)
_MAX_NATIVE_IDENTIFIER_BYTES = 256
_MAX_NATIVE_AUDIO_BYTES = (1 << 63) - 1


def _validate_native_identifier(value: object) -> str:
    if type(value) is not str or not value or len(value) > _MAX_NATIVE_IDENTIFIER_BYTES:
        raise RuntimeError("invalid native response event")
    if value.strip() != value:
        raise RuntimeError("invalid native response event")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RuntimeError("invalid native response event") from exc
    if len(encoded) > _MAX_NATIVE_IDENTIFIER_BYTES:
        raise RuntimeError("invalid native response event")
    return value


def _validate_native_event(
    event: RealtimeVoiceEvent, *, max_transcript_chars: int
) -> None:
    event_type = type(event)
    if event_type not in _NATIVE_EVENT_TYPES:
        if isinstance(event, _NATIVE_EVENT_TYPES):
            raise RuntimeError("exact native event type required")
        raise RuntimeError("invalid native response event")
    if event_type is ResponseStarted:
        _validate_native_identifier(event.response_id)
        _validate_native_identifier(event.turn_id)
        if event.continuation_of_batch_id is not None:
            raise RuntimeError("invalid native response event")
        return
    if event_type is ResponseCompleted:
        _validate_native_identifier(event.response_id)
        _validate_native_identifier(event.turn_id)
        if event.continuation_of_batch_id is not None:
            raise RuntimeError("invalid native response event")
        return
    _validate_native_identifier(event.item_id)
    _validate_native_identifier(event.turn_id)
    _validate_native_identifier(event.response_id)
    if event_type is OutputAudio:
        if type(event.data) is not bytes or not event.data:
            raise RuntimeError("invalid native response event")
        return
    if (
        type(event.text) is not str
        or len(event.text) > max_transcript_chars
        or type(event.final) is not bool
        or event.final is not True
        or event.role is not TranscriptRole.ASSISTANT
        or event.provenance is not TranscriptProvenance.ASSISTANT_OUTPUT_AUDIO
    ):
        raise RuntimeError("invalid native response event")
    try:
        encoded_text = event.text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RuntimeError("invalid native response event") from exc
    if len(encoded_text) > max_transcript_chars:
        raise RuntimeError("invalid native response event")


@dataclass(slots=True)
class _NativeResponseState:
    request: object
    generation: int
    interrupt_generation: int
    lease_id: str
    response_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    successful_audio_bytes: int = 0
    lease: object | None = None
    acquire_task: asyncio.Task[object] | None = None
    operation_task: asyncio.Task[object] | None = None
    terminal_task: asyncio.Task[bool] | None = None
    terminal_intent: str | None = None
    provider_completed: bool = False
    speaking: bool = False
    interrupted: bool = False
    cancel_sent: bool = False
    lease_terminal_started: bool = False
    receipt_validated: bool = False
    retired: bool = False


@dataclass(slots=True)
class _BargeInBarrier:
    response: _NativeResponseState
    transport_generation: int
    interrupt_generation: int
    speech_item_id: str
    provider_terminal: asyncio.Event
    playback_terminal: asyncio.Event
    host_terminal: asyncio.Event
    transcript: InputTranscript | None = None
    worker: asyncio.Task[None] | None = None


class GatewayRealtimeVoiceController:
    """Own provider transport while leaving canonical turn authority with its host."""

    def __init__(
        self,
        host: RealtimeControllerHost,
        *,
        audio_queue_size: int = 32,
        replay_capacity: int = 1024,
        max_transcript_chars: int = 32_768,
        interrupt_timeout: float = 10.0,
    ) -> None:
        if isinstance(audio_queue_size, bool) or not isinstance(audio_queue_size, int):
            raise TypeError("audio_queue_size must be a positive integer")
        if audio_queue_size <= 0:
            raise ValueError("audio_queue_size must be a positive integer")
        for name, value in (
            ("replay_capacity", replay_capacity),
            ("max_transcript_chars", max_transcript_chars),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be a positive integer")
            if value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if isinstance(interrupt_timeout, bool) or not isinstance(
            interrupt_timeout, (int, float)
        ):
            raise TypeError("interrupt_timeout must be a positive finite number")
        if (
            interrupt_timeout <= 0
            or interrupt_timeout == float("inf")
            or interrupt_timeout != interrupt_timeout
        ):
            raise ValueError("interrupt_timeout must be a positive finite number")
        self._host = host
        self._audio_queue: asyncio.Queue[tuple[bytes, str | None]] = asyncio.Queue(
            audio_queue_size
        )
        self._replay_capacity = replay_capacity
        self._max_transcript_chars = max_transcript_chars
        self._interrupt_timeout = interrupt_timeout
        self._binding: RealtimeSessionBinding | None = None
        self._session: RealtimeVoiceSession | None = None
        self._admission: FinalTranscriptAdmission[Any, Any] | None = None
        self._admission_lock = asyncio.Lock()
        self._transport_generation = 0
        self._interrupt_generation = 0
        self._resume_token: str | None = None
        self._reconnecting = False
        self._sequence = 0
        self._lifecycle_events: list[ControllerEvent] = []
        self._event_task: asyncio.Task[None] | None = None
        self._audio_task: asyncio.Task[None] | None = None
        self._open_task: asyncio.Task[RealtimeVoiceSession] | None = None
        self._closing = False
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._resume_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._native_response: _NativeResponseState | None = None
        self._native_lock = asyncio.Lock()
        self._native_terminal_task: asyncio.Task[bool] | None = None
        self._barge_in_barrier: _BargeInBarrier | None = None
        self._native_tombstone_order: deque[tuple[str, str]] = deque()
        self._native_tombstone_ids: set[tuple[str, str]] = set()

    @property
    def lifecycle_events(self) -> tuple[ControllerEvent, ...]:
        return tuple(self._lifecycle_events)

    def _emit(
        self,
        lifecycle: ControllerLifecycle,
        *,
        detail: str = "",
        provider_event: RealtimeVoiceEvent | None = None,
        generation: int | None = None,
        admission_status: AdmissionStatus | None = None,
    ) -> None:
        binding = self._binding
        if binding is None:
            return
        self._sequence += 1
        self._lifecycle_events.append(
            ControllerEvent(
                sequence=self._sequence,
                lifecycle=lifecycle,
                binding=binding,
                transport_generation=(
                    self._transport_generation if generation is None else generation
                ),
                detail=detail,
                provider_event=provider_event,
                admission_status=admission_status,
            )
        )

    async def open(
        self,
        provider_name: str,
        setup: RealtimeVoiceSetup,
        binding: RealtimeSessionBinding,
        *,
        required_capabilities: frozenset[RealtimeCapability] = frozenset(),
    ) -> None:
        if self._binding is not None:
            raise RuntimeError("controller already owns an attachment")
        if not all(
            callable(getattr(self._host, method_name, None))
            for method_name in _REQUIRED_NATIVE_HOST_METHODS
        ):
            raise RuntimeError("native host API is incomplete")
        self._binding = binding
        self._emit(ControllerLifecycle.CONNECTING)
        native_required_capabilities = (
            frozenset(required_capabilities) | _REQUIRED_NATIVE_CAPABILITIES
        )
        self._open_task = asyncio.create_task(
            open_realtime_voice_session(
                provider_name,
                setup,
                required_capabilities=native_required_capabilities,
            )
        )
        try:
            session = await asyncio.shield(self._open_task)
        except asyncio.CancelledError:
            while True:
                try:
                    session = await asyncio.shield(self._open_task)
                    self._session = session
                    break
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            await self.close(reason="open cancelled")
            raise
        except BaseException:
            self._emit(ControllerLifecycle.FAILED, detail="open failed")
            await self.close(reason="open failed")
            raise
        self._session = session
        if self._closing or self._closed:
            await self.close(reason="closed during open")
            return
        session_capabilities = session.capabilities
        if (
            type(session_capabilities) is not frozenset
            or any(
                type(capability) is not RealtimeCapability
                for capability in session_capabilities
            )
            or not _REQUIRED_NATIVE_CAPABILITIES.issubset(session_capabilities)
        ):
            detail = "native realtime session incompatible"
            self._emit(ControllerLifecycle.FAILED, detail=detail)
            await self.close(reason=detail)
            raise RuntimeError(detail)
        self._admission = FinalTranscriptAdmission(
            binding,
            self._host,
            self._host,
            replay_capacity=self._replay_capacity,
            max_transcript_chars=self._max_transcript_chars,
            clock=time.monotonic,
        )
        self._transport_generation = 1
        self._event_task = asyncio.create_task(self._pump_events(1))
        self._audio_task = asyncio.create_task(self._pump_audio())

    async def _pump_events(self, generation: int) -> None:
        session = self._session
        assert session is not None
        try:
            async for event in session.events():
                await self._handle_event(event, generation)
                if self._reconnecting or self._closing:
                    return
            if not self._closing and not self._reconnecting:
                self._emit(
                    ControllerLifecycle.FAILED,
                    detail="provider event stream exhausted",
                    generation=generation,
                )
                await self.close(reason="provider event stream exhausted")
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if not self._closing and generation == self._transport_generation:
                detail = (
                    "native response event failed"
                    if self._native_response is not None
                    else "provider event stream failed"
                )
                self._emit(
                    ControllerLifecycle.FAILED, detail=detail, generation=generation
                )
                await self.close(reason="event stream failure")

    async def _handle_event(self, event: RealtimeVoiceEvent, generation: int) -> None:
        if self._closing or generation != self._transport_generation:
            return
        if isinstance(event, SessionReady):
            self._resume_token = event.session_id
            self._reconnecting = False
            self._emit(
                ControllerLifecycle.READY,
                provider_event=event,
                generation=generation,
            )
            self._emit(ControllerLifecycle.LISTENING, generation=generation)
            return
        if isinstance(event, SessionFailure):
            if self._native_response is not None:
                self._emit(
                    ControllerLifecycle.FAILED,
                    detail="native provider failure",
                    provider_event=event,
                    generation=generation,
                )
                await self._native_failure("native provider failure")
                return
            session = self._session
            if (
                session is not None
                and RealtimeCapability.SESSION_RESUMPTION in session.capabilities
                and self._resume_token is not None
            ):
                self._reconnecting = True
                await self._stop_audio_for_reconnect()
                self._emit(
                    ControllerLifecycle.RECONNECTING,
                    detail=event.message,
                    provider_event=event,
                    generation=generation,
                )
                return
            self._emit(
                ControllerLifecycle.FAILED,
                detail=event.message,
                provider_event=event,
                generation=generation,
            )
            await self.close(reason="terminal provider failure")
            return
        if isinstance(event, SessionClosed):
            lifecycle = (
                self._lifecycle_events[-1].lifecycle
                if self._lifecycle_events
                else ControllerLifecycle.CONNECTING
            )
            self._emit(
                lifecycle,
                detail=event.reason,
                provider_event=event,
                generation=generation,
            )
            await self.close(reason=event.reason or "provider closed")
            return
        if type(event) is InputSpeechStarted:
            await self._handle_speech_started(event, generation)
            return
        if isinstance(event, InputTranscript):
            fence = self._interrupt_generation
            async with self._admission_lock:
                if (
                    self._closing
                    or generation != self._transport_generation
                    or fence != self._interrupt_generation
                ):
                    return
                admission = self._admission
                assert admission is not None
                result = await admission.admit(event)
                if result.status is AdmissionStatus.SUBMITTED:
                    await self._start_native_response(
                        result.receipt, event, generation, fence
                    )

                elif result.status is not AdmissionStatus.IGNORED_PARTIAL:
                    self._emit(
                        self._lifecycle_events[-1].lifecycle,
                        detail=f"admission {result.status.value}",
                        provider_event=event,
                        generation=generation,
                        admission_status=result.status,
                    )
            return
        if type(event) in _NATIVE_EVENT_TYPES:
            await self._handle_native_event(event, generation)
            return
        if isinstance(event, _NATIVE_EVENT_TYPES):
            raise RuntimeError("exact native event type required")
        # Provider tools and all other events are projection-only in this slice.
        self._emit(
            self._lifecycle_events[-1].lifecycle,
            provider_event=event,
            generation=generation,
        )

    async def _handle_speech_started(
        self, event: InputSpeechStarted, generation: int
    ) -> None:
        async with self._native_lock:
            state = self._native_response
            if (
                state is None
                or generation != state.generation
                or generation != self._transport_generation
            ):
                return
            barrier = self._barge_in_barrier
            if barrier is not None and barrier.response is state:
                return
            self._interrupt_generation += 1
            terminal_task = self._claim_native_terminal_locked(state, "interrupt")
            barrier = _BargeInBarrier(
                response=state,
                transport_generation=generation,
                interrupt_generation=self._interrupt_generation,
                speech_item_id=event.item_id,
                provider_terminal=asyncio.Event(),
                playback_terminal=asyncio.Event(),
                host_terminal=asyncio.Event(),
            )
            self._barge_in_barrier = barrier
            worker = asyncio.create_task(
                self._run_barge_in_barrier(barrier, terminal_task)
            )
            barrier.worker = worker
            worker.add_done_callback(self._observe_native_operation)

    async def _run_barge_in_barrier(
        self, barrier: _BargeInBarrier, terminal_task: asyncio.Task[bool]
    ) -> None:
        binding = self._binding
        if binding is None:
            return
        host_task = self._create_native_task(
            self._host.interrupt_and_wait(binding, self._interrupt_timeout)
        )
        terminal_result, host_result = await asyncio.gather(
            asyncio.shield(terminal_task),
            asyncio.shield(host_task),
            return_exceptions=True,
        )
        barrier.provider_terminal.set()
        barrier.playback_terminal.set()
        barrier.host_terminal.set()
        if (
            terminal_result is True
            or isinstance(terminal_result, BaseException)
            or isinstance(host_result, BaseException)
        ):
            await self._request_close("native response cleanup failed")

    async def _start_native_response(
        self,
        receipt: object | None,
        event: InputTranscript,
        generation: int,
        interrupt_generation: int,
    ) -> None:
        session, binding = self._session, self._binding
        if receipt is None or session is None or binding is None:
            await self._native_failure("native output reservation failed")
            return
        if not _REQUIRED_NATIVE_CAPABILITIES.issubset(session.capabilities):
            await self._native_failure("native response unavailable")
            return
        try:
            request = await self._host.reserve_native_output(binding, receipt)
            if self._host.validate_native_output_request(request) is not request:
                raise RuntimeError("native request identity changed")
            reservation = request.reservation
            provider_request = RealtimeResponseRequest(
                durable_session_id=reservation.durable_session_id,
                assistant_message_id=reservation.assistant_message_id,
                turn_marker=reservation.turn_marker,
                canonical_text=request.canonical_text,
                content_digest=reservation.content_digest,
                output_audio_format=reservation.output_audio_format,
                allow_tools=False,
            )
            state = _NativeResponseState(
                request, generation, interrupt_generation, uuid.uuid4().hex
            )
            async with self._native_lock:
                if self._native_response is not None or self._closing:
                    raise RuntimeError("native response unavailable")
                self._native_response = state
                operation = self._create_native_task(
                    session.start_response(provider_request)
                )
                state.operation_task = operation
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                async with self._native_lock:
                    expected = state.interrupted or state.terminal_task is not None
                if not expected:
                    raise
                return
            finally:
                async with self._native_lock:
                    if state.operation_task is operation and operation.done():
                        state.operation_task = None
            async with self._native_lock:
                active = (
                    self._native_response is state
                    and not state.interrupted
                    and state.terminal_task is None
                )
            if not active:
                return
            self._emit(
                ControllerLifecycle.QUEUED,
                detail="native response queued",
                provider_event=event,
                generation=generation,
                admission_status=AdmissionStatus.SUBMITTED,
            )
            self._emit(ControllerLifecycle.THINKING, detail="native response pending")
        except BaseException:
            await self._native_failure("native response start failed")

    async def _handle_native_event(
        self, event: RealtimeVoiceEvent, generation: int
    ) -> None:
        _validate_native_event(event, max_transcript_chars=self._max_transcript_chars)
        async with self._native_lock:
            state = self._native_response
            event_identity = (event.response_id, event.turn_id)
            if state is None and event_identity in self._native_tombstone_ids:
                raise RuntimeError("late duplicate native response event")
            if (
                state is None
                or state.interrupted
                or generation != state.generation
                or generation != self._transport_generation
            ):
                raise RuntimeError("unsolicited or stale native response event")
            if type(event) is ResponseStarted:
                if state.response_id is not None:
                    raise RuntimeError("duplicate native response start")
                authenticated = self._host.validate_native_output_request(state.request)
                if authenticated is not state.request:
                    raise RuntimeError("native response identity mismatch")
                turn_marker = _validate_native_identifier(
                    authenticated.reservation.turn_marker
                )
                if event.turn_id != turn_marker:
                    raise RuntimeError("native response identity mismatch")
                state.response_id, state.turn_id = event.response_id, event.turn_id
                acquire_task = self._create_native_task(
                    self._host.acquire_native_playback(
                        state.request,
                        lease_id=state.lease_id,
                        response_id=event.response_id,
                        transport_generation=generation,
                    )
                )
                state.acquire_task = acquire_task
                state.operation_task = acquire_task
                operation = None
                terminal_task = None
            else:
                acquire_task = None
                lease = state.lease
                if state.response_id is None or lease is None:
                    raise RuntimeError("native response event before start")
                if event_identity != (state.response_id, state.turn_id):
                    raise RuntimeError("native response identity mismatch")
                if state.provider_completed:
                    raise RuntimeError("native response event after completion")
                if type(event) in (OutputAudio, OutputTranscript):
                    if state.item_id is None:
                        state.item_id = event.item_id
                    elif event.item_id != state.item_id:
                        raise RuntimeError("native response item mismatch")
                if type(event) is OutputTranscript:
                    self._emit(
                        self._lifecycle_events[-1].lifecycle,
                        detail="native output transcript",
                    )
                    return
                if type(event) is OutputAudio:
                    if len(event.data) > _MAX_NATIVE_AUDIO_BYTES - state.successful_audio_bytes:
                        raise RuntimeError("native response audio limit exceeded")
                    operation = self._create_native_task(lease.write_pcm(event.data))
                    state.operation_task = operation
                    terminal_task = None
                else:
                    if state.successful_audio_bytes == 0:
                        raise RuntimeError("native response missing audio")
                    state.provider_completed = True
                    terminal_task = self._claim_native_terminal_locked(state, "drain")
                    operation = None

        if acquire_task is not None:
            try:
                acquired = await asyncio.shield(acquire_task)
            except asyncio.CancelledError:
                async with self._native_lock:
                    expected = state.interrupted or state.terminal_task is not None
                if expected:
                    return
                raise
            except BaseException:
                async with self._native_lock:
                    expected = state.interrupted or state.terminal_task is not None
                if expected:
                    return
                await self._native_failure("native playback acquisition failed")
                return
            finally:
                async with self._native_lock:
                    if state.operation_task is acquire_task and acquire_task.done():
                        state.operation_task = None
            async with self._native_lock:
                if self._native_response is state:
                    state.lease = acquired
            return

        if operation is not None:
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                async with self._native_lock:
                    expected = state.interrupted or state.terminal_task is not None
                if expected:
                    return
                raise
            except BaseException:
                async with self._native_lock:
                    expected = state.interrupted or state.terminal_task is not None
                if expected:
                    return
                await self._native_failure("native playback write failed")
                return
            finally:
                async with self._native_lock:
                    if state.operation_task is operation and operation.done():
                        state.operation_task = None
            async with self._native_lock:
                active = self._native_response is state and not state.interrupted
                if active:
                    state.successful_audio_bytes += len(event.data)
                emit_speaking = active and not state.speaking
                if emit_speaking:
                    state.speaking = True
            if emit_speaking:
                self._emit(
                    ControllerLifecycle.SPEAKING,
                    detail="native audio streaming",
                )
            return

        assert terminal_task is not None
        if asyncio.current_task() is not self._event_task:
            cleanup_failed = await asyncio.shield(terminal_task)
            if cleanup_failed:
                await self._request_close("native response cleanup failed")

    def _claim_native_terminal_locked(
        self, state: _NativeResponseState, intent: str
    ) -> asyncio.Task[bool]:
        if state.terminal_intent is None or (
            state.terminal_intent == "drain" and intent != "drain"
        ):
            state.terminal_intent = intent
        if intent != "drain":
            state.interrupted = True
            operation = state.operation_task
            if (
                operation is not None
                and operation is not state.acquire_task
                and not operation.done()
            ):
                operation.cancel()
        if state.terminal_task is None:
            task = asyncio.create_task(self._terminalize_native(state))
            state.terminal_task = task
            self._native_terminal_task = task
            task.add_done_callback(self._observe_native_terminal)
        return state.terminal_task

    async def _claim_native_terminal(self, intent: str) -> asyncio.Task[bool] | None:
        async with self._native_lock:
            state = self._native_response
            if state is None:
                return None
            return self._claim_native_terminal_locked(state, intent)

    @staticmethod
    def _observe_native_terminal(task: asyncio.Task[bool]) -> None:
        try:
            task.result()
        except BaseException:
            pass

    @staticmethod
    def _observe_native_operation(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except BaseException:
            pass

    def _create_native_task(self, awaitable: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(awaitable)
        task.add_done_callback(self._observe_native_operation)
        return task

    async def _await_native_task(self, task: asyncio.Task[Any]) -> Any:
        done, _pending = await asyncio.wait({task}, timeout=self._interrupt_timeout)
        if not done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise TimeoutError("native operation timed out")
        return task.result()

    async def _terminalize_native(self, state: _NativeResponseState) -> bool:
        cleanup_failed = False
        receipt: object | None = None
        terminal_method: str | None = None
        lease = state.lease
        session = self._session

        try:
            authenticated = self._host.validate_native_output_request(state.request)
            if authenticated is not state.request:
                raise RuntimeError("native request identity changed")
        except BaseException:
            cleanup_failed = True

        async with self._native_lock:
            intent = state.terminal_intent or "failure"
            response_id = state.response_id
            send_cancel = (
                intent != "drain" and response_id is not None and not state.cancel_sent
            )
            if send_cancel:
                state.cancel_sent = True
        if send_cancel and session is not None:
            cancel_task = self._create_native_task(session.cancel_response(response_id))
            try:
                await self._await_native_task(cancel_task)
            except BaseException:
                cleanup_failed = True

        acquire_task = state.acquire_task
        if lease is None and acquire_task is not None:
            try:
                lease = await self._await_native_task(acquire_task)
            except BaseException:
                cleanup_failed = True
            else:
                async with self._native_lock:
                    if self._native_response is state:
                        state.lease = lease

        async with self._native_lock:
            intent = state.terminal_intent or "failure"
            response_id = state.response_id
            send_cancel = (
                intent != "drain" and response_id is not None and not state.cancel_sent
            )
            if send_cancel:
                state.cancel_sent = True
        if send_cancel and session is not None:
            cancel_task = self._create_native_task(session.cancel_response(response_id))
            try:
                await self._await_native_task(cancel_task)
            except BaseException:
                cleanup_failed = True

        if lease is not None:
            async with self._native_lock:
                intent = state.terminal_intent or "failure"
            if intent == "drain":
                terminal_method = "finish"
                finish_task = self._create_native_task(
                    lease.finish_and_wait(self._interrupt_timeout)
                )
                async with self._native_lock:
                    state.operation_task = finish_task
                    state.lease_terminal_started = True
                try:
                    receipt = await self._await_native_task(finish_task)
                except asyncio.CancelledError:
                    async with self._native_lock:
                        switched = state.terminal_intent != "drain"
                    if not switched:
                        cleanup_failed = True
                except BaseException:
                    cleanup_failed = True
                finally:
                    async with self._native_lock:
                        if state.operation_task is finish_task:
                            state.operation_task = None

            async with self._native_lock:
                intent = state.terminal_intent or "failure"
                response_id = state.response_id
                send_cancel = (
                    intent != "drain"
                    and response_id is not None
                    and not state.cancel_sent
                )
                if send_cancel:
                    state.cancel_sent = True
            if send_cancel and session is not None:
                cancel_task = self._create_native_task(session.cancel_response(response_id))
                try:
                    await self._await_native_task(cancel_task)
                except BaseException:
                    cleanup_failed = True
            if receipt is None and intent != "drain":
                terminal_method = "interrupt"
                interrupt_task = self._create_native_task(
                    lease.interrupt_and_wait(self._interrupt_timeout)
                )
                async with self._native_lock:
                    state.operation_task = interrupt_task
                    state.lease_terminal_started = True
                try:
                    receipt = await self._await_native_task(interrupt_task)
                except BaseException:
                    cleanup_failed = True
                finally:
                    async with self._native_lock:
                        if state.operation_task is interrupt_task:
                            state.operation_task = None

        if receipt is not None and lease is not None:
            try:
                validated = self._host.validate_native_playback_receipt(
                    state.request, lease, receipt
                )
                expected_interrupted = terminal_method == "interrupt"
                if (
                    validated is not receipt
                    or type(getattr(receipt, "interrupted", None)) is not bool
                    or receipt.interrupted is not expected_interrupted
                ):
                    raise RuntimeError("invalid native playback receipt")
                state.receipt_validated = True
                retired = await self._host.retire_native_output(
                    state.request, lease, receipt
                )
                if retired is not receipt:
                    raise RuntimeError("native retirement identity changed")
                state.retired = True
            except BaseException:
                cleanup_failed = True
        elif lease is not None:
            cleanup_failed = True

        if cleanup_failed and lease is not None:
            try:
                close_task = self._create_native_task(lease.close())
                await self._await_native_task(close_task)
            except BaseException:
                pass

        async with self._native_lock:
            if state.response_id is not None and state.turn_id is not None:
                identity = (state.response_id, state.turn_id)
                if identity not in self._native_tombstone_ids:
                    if len(self._native_tombstone_order) >= self._replay_capacity:
                        expired = self._native_tombstone_order.popleft()
                        self._native_tombstone_ids.remove(expired)
                    self._native_tombstone_order.append(identity)
                    self._native_tombstone_ids.add(identity)
            if self._native_response is state:
                self._native_response = None

        if cleanup_failed:
            self._emit(
                ControllerLifecycle.FAILED,
                detail="native response cleanup failed",
            )
        elif not self._closing:
            if terminal_method == "finish":
                self._emit(
                    ControllerLifecycle.COMPLETED,
                    detail="native playback drained",
                )
            else:
                self._emit(
                    ControllerLifecycle.LISTENING,
                    detail="native response interrupted",
                )
        return cleanup_failed

    async def _native_failure(self, detail: str) -> None:
        if not self._closing:
            self._emit(ControllerLifecycle.FAILED, detail=detail)
        terminal_task = await self._claim_native_terminal("failure")
        cleanup_failed = (
            await asyncio.shield(terminal_task) if terminal_task is not None else False
        )
        await self._request_close(
            "native response cleanup failed" if cleanup_failed else detail
        )

    async def _pump_audio(self) -> None:
        try:
            while True:
                audio, mime_type = await self._audio_queue.get()
                try:
                    session = self._session
                    if session is not None and not self._closing:
                        await session.send_audio(audio, mime_type=mime_type)
                finally:
                    self._audio_queue.task_done()
        except asyncio.CancelledError:
            raise
        except BaseException:
            if not self._closing:
                self._emit(ControllerLifecycle.FAILED, detail="audio send failed")
                await self.close(reason="audio send failed")

    async def _stop_audio_for_reconnect(self) -> None:
        task = self._audio_task
        self._audio_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        while True:
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._audio_queue.task_done()

    def feed_audio(
        self, data: bytes | bytearray | memoryview, *, mime_type: str | None = None
    ) -> AudioFeedResult:
        if self._reconnecting:
            self._emit(
                ControllerLifecycle.RECONNECTING,
                detail="audio rejected while reconnecting",
            )
            return AudioFeedResult.RECONNECTING
        if self._closing or self._closed or self._session is None:
            return AudioFeedResult.CLOSED
        copied = bytes(data)
        try:
            self._audio_queue.put_nowait((copied, mime_type))
        except asyncio.QueueFull:
            lifecycle = (
                self._lifecycle_events[-1].lifecycle
                if self._lifecycle_events
                else ControllerLifecycle.CONNECTING
            )
            self._emit(lifecycle, detail="audio queue overflow")
            return AudioFeedResult.OVERFLOW
        return AudioFeedResult.ACCEPTED

    async def interrupt(self) -> None:
        self._interrupt_generation += 1
        failure: BaseException | None = None
        async with self._admission_lock:
            try:
                session = self._session
                binding = self._binding
                if session is None or binding is None or self._closing:
                    return
                terminal_task = await self._claim_native_terminal("interrupt")
                if terminal_task is not None:
                    cleanup_failed = await asyncio.shield(terminal_task)
                    if cleanup_failed:
                        failure = RuntimeError("native response cleanup failed")
                else:
                    if RealtimeCapability.EXPLICIT_INTERRUPTION in session.capabilities:
                        await session.interrupt()
                    await self._host.interrupt_and_wait(
                        binding, self._interrupt_timeout
                    )
            except BaseException as exc:
                # Fence admission before releasing the barrier. Scheduling close()
                # first would leave one event-loop turn where a final transcript
                # could authorize against an attachment already known to be unsafe.
                self._closing = True
                failure = exc
        if failure is not None:
            reason = (
                "native response cleanup failed"
                if str(failure) == "native response cleanup failed"
                else "interrupt failed"
            )
            await self._request_close(reason)
            raise failure

    async def resume(self, *, timeout: float = 10.0) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout must be a positive finite number")
        if timeout <= 0 or timeout == float("inf") or timeout != timeout:
            raise ValueError("timeout must be a positive finite number")
        async with self._resume_lock:
            await self._resume_owned(timeout)

    async def _resume_owned(self, timeout: float) -> None:
        if not self._reconnecting:
            raise RuntimeError("session is not reconnecting")
        session = self._session
        token = self._resume_token
        if session is None or token is None:
            raise RuntimeError("session has no native resume token")
        prior_pump = self._event_task
        if prior_pump is not None and prior_pump is not asyncio.current_task():
            # Joining the retained pump is controller cleanup, not caller-owned work.
            # Shield it so cancellation of one resume waiter cannot poison all
            # subsequent resume attempts by cancelling the shared pump task.
            await asyncio.shield(prior_pump)
        try:
            await asyncio.wait_for(session.resume_session(token), timeout=timeout)
        except BaseException:
            await self.close(reason="resume failed")
            raise
        if self._closing or self._closed:
            return
        self._transport_generation += 1
        generation = self._transport_generation
        self._reconnecting = False
        self._emit(ControllerLifecycle.CONNECTING, generation=generation)
        self._event_task = asyncio.create_task(self._pump_events(generation))
        self._audio_task = asyncio.create_task(self._pump_audio())

    async def project_host(self, projection: HostProjection) -> None:
        if projection.binding != self._binding or self._closing or self._closed:
            raise RuntimeError("host projection does not match the live attachment")
        if self._native_response is not None and projection.status in {
            HostProjectionStatus.SPEAKING,
            HostProjectionStatus.COMPLETED,
        }:
            raise RuntimeError("native response owns lifecycle")
        if projection.status is HostProjectionStatus.COMPLETED and (
            projection.finalization is None
            or not self._host.validate_finalization(projection.finalization)
        ):
            raise RuntimeError(
                "completed projection requires canonical persistence receipt"
            )
        current = self._lifecycle_events[-1].lifecycle
        rank = {
            ControllerLifecycle.THINKING: 0,
            ControllerLifecycle.ACTING: 1,
            ControllerLifecycle.SPEAKING: 2,
            ControllerLifecycle.COMPLETED: 3,
            ControllerLifecycle.FAILED: 3,
        }
        projected = ControllerLifecycle(projection.status.value)
        if (
            current in rank and (rank[projected] < rank[current] or rank[current] == 3)
        ) or (current not in rank and projected is not ControllerLifecycle.THINKING):
            raise RuntimeError(
                "host projection is not a monotonic canonical transition"
            )
        self._emit(projected, detail=projection.detail)

    async def close(self, *, reason: str = "") -> None:
        await self._request_close(reason)

    async def _request_close(self, reason: str) -> None:
        async with self._close_lock:
            if self._closed:
                return
            close_task = self._close_task
            if close_task is None or (
                close_task.done()
                and (close_task.cancelled() or close_task.exception() is not None)
            ):
                self._closing = True
                close_task = asyncio.create_task(self._finish_close(reason))
                self._close_task = close_task
                close_task.add_done_callback(self._observe_close_task)
        if asyncio.current_task() in {self._event_task, self._audio_task}:
            return
        await asyncio.shield(close_task)

    @staticmethod
    def _observe_close_task(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except BaseException:
            pass

    async def _finish_close(self, reason: str) -> None:
        """Own cleanup independently of any one caller waiting for close."""
        if self._closed:
            return
        cleanup_detail: str | None = None
        open_task = self._open_task
        if open_task is not None and open_task is not asyncio.current_task():
            try:
                late_session = await asyncio.shield(open_task)
            except BaseException:
                late_session = None
            if late_session is not None and self._session is None:
                self._session = late_session
        terminal_task = await self._claim_native_terminal("close")
        if terminal_task is not None:
            try:
                if await asyncio.shield(terminal_task):
                    cleanup_detail = "native response cleanup failed"
            except BaseException:
                cleanup_detail = "native response cleanup failed"
        admission = self._admission
        if admission is not None:
            try:
                await admission.close()
            except BaseException:
                if cleanup_detail is None:
                    cleanup_detail = "controller cleanup failed"
        current = asyncio.current_task()
        tasks = [
            task
            for task in (self._event_task, self._audio_task)
            if task is not None and task is not current
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        session = self._session
        provider_cleanup_failure: BaseException | None = None
        if session is not None:
            try:
                await session.close()
            except BaseException as exc:
                provider_cleanup_failure = exc
                cleanup_detail = "provider cleanup failed"
        close_attachment = getattr(self._host, "close_attachment", None)
        host_cleanup_failure: BaseException | None = None
        if callable(close_attachment):
            try:
                await close_attachment(self._binding)
            except BaseException as exc:
                host_cleanup_failure = exc
                cleanup_detail = "native cleanup failed"
        final_detail = (
            "closed after cleanup failure"
            if host_cleanup_failure is not None
            else cleanup_detail or reason
        )
        if cleanup_detail is not None and not (
            self._lifecycle_events
            and self._lifecycle_events[-1].lifecycle is ControllerLifecycle.FAILED
            and self._lifecycle_events[-1].detail == cleanup_detail
        ):
            self._emit(ControllerLifecycle.FAILED, detail=cleanup_detail)
        if provider_cleanup_failure is not None:
            raise provider_cleanup_failure
        self._emit(ControllerLifecycle.CLOSING, detail=final_detail)
        self._closed = True
        self._emit(ControllerLifecycle.CLOSED, detail=final_detail)
        if host_cleanup_failure is not None:
            raise host_cleanup_failure


__all__ = [
    "AudioFeedResult",
    "ControllerEvent",
    "ControllerLifecycle",
    "GatewayRealtimeVoiceController",
    "HostProjection",
    "HostProjectionStatus",
    "RealtimeControllerHost",
]
