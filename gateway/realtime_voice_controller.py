"""Provider-neutral lifecycle owner for one gateway realtime voice attachment."""

from __future__ import annotations

import asyncio
import time
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
    InputTranscript,
    Interruption,
    MAX_IDENTIFIER_LENGTH,
    OutputAudio,
    OutputTranscript,
    RealtimeCapability,
    RealtimeOutputAudioFormat,
    RealtimeResponseRequest,
    RealtimeVoiceEvent,
    RealtimeVoiceSession,
    RealtimeVoiceSetup,
    SessionClosed,
    SessionFailure,
    SessionReady,
    ResponseCompleted,
    ResponseStarted,
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
    NATIVE_RESERVED = "native_reserved"
    NATIVE_STARTED = "native_started"
    NATIVE_PLAYING = "native_playing"
    NATIVE_DRAINING = "native_draining"
    NATIVE_DRAINED = "native_drained"
    NATIVE_FAILED = "native_failed"
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


def _validate_native_identifier(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > MAX_IDENTIFIER_LENGTH
    ):
        raise ValueError(f"{name} must be an exact bounded identifier")


@dataclass(frozen=True, slots=True)
class NativePlaybackReceipt:
    lease_id: str
    response_id: str
    turn_marker: str
    transport_generation: int
    bytes_written: int
    interrupted: bool

    def __post_init__(self) -> None:
        for name in ("lease_id", "response_id", "turn_marker"):
            _validate_native_identifier(getattr(self, name), name)
        for name in ("transport_generation", "bytes_written"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative exact int")
        if type(self.interrupted) is not bool:
            raise TypeError("interrupted must be an exact bool")


class NativeAudioPlaybackLease(Protocol):
    @property
    def lease_id(self) -> str: ...

    @property
    def response_id(self) -> str: ...

    @property
    def turn_marker(self) -> str: ...

    @property
    def transport_generation(self) -> int: ...

    async def write_pcm(self, data: bytes) -> None: ...

    async def finish_and_wait(self, timeout: float) -> NativePlaybackReceipt: ...

    async def interrupt_and_wait(self, timeout: float) -> NativePlaybackReceipt: ...

    async def close(self) -> None: ...


class NativeAudioOutputSink(Protocol):
    async def open_lease(
        self,
        binding: RealtimeSessionBinding,
        response_id: str,
        turn_marker: str,
        output_format: RealtimeOutputAudioFormat,
        transport_generation: int,
    ) -> NativeAudioPlaybackLease: ...

    def validate_playback_receipt(
        self, lease: NativeAudioPlaybackLease, receipt: NativePlaybackReceipt
    ) -> bool: ...


@dataclass(slots=True)
class _NativeOutputState:
    durable_session_id: str
    assistant_message_id: int
    turn_marker: str
    transport_generation: int
    content_digest: str | None = None
    output_format: RealtimeOutputAudioFormat | None = None
    send_accepted: bool = False
    response_id: str | None = None
    lease: NativeAudioPlaybackLease | None = None
    lease_id: str | None = None
    item_id: str | None = None
    bytes_written: int = 0
    playing: bool = False
    provider_completed: bool = False
    lease_closed: bool = False


class RealtimeControllerHost(Protocol):
    async def authorize(self, binding: RealtimeSessionBinding, utterance: Any) -> Any: ...

    async def revoke(self, permit: Any) -> None: ...

    async def submit(
        self, binding: RealtimeSessionBinding, utterance: Any, permit: Any
    ) -> Any: ...

    async def interrupt_and_wait(
        self, binding: RealtimeSessionBinding, timeout: float
    ) -> None: ...

    def validate_finalization(self, receipt: object) -> bool: ...

    async def claim_native_response(
        self, binding: RealtimeSessionBinding, finalization: object
    ) -> RealtimeResponseRequest: ...


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
        output_sink: NativeAudioOutputSink | None = None,
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
        self._output_sink = output_sink
        self._output_lock = asyncio.Lock()
        self._native_output: _NativeOutputState | None = None
        self._output_send_task: asyncio.Task[None] | None = None
        self._output_drain_task: asyncio.Task[None] | None = None
        self._native_failure: str | None = None
        self._native_failure_owner: asyncio.Task[Any] | None = None
        self._host_projection_lifecycle: ControllerLifecycle | None = None
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
        self._binding = binding
        self._emit(ControllerLifecycle.CONNECTING)
        self._open_task = asyncio.create_task(open_realtime_voice_session(
            provider_name, setup, required_capabilities=required_capabilities,
        ))
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
                self._emit(ControllerLifecycle.FAILED, detail=str(exc), generation=generation)
                await self.close(reason="event stream failure")

    async def _handle_event(
        self, event: RealtimeVoiceEvent, generation: int
    ) -> None:
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
            await self._fence_native_output()
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
        if isinstance(event, ResponseStarted):
            await self._start_native_playback(event, generation)
            return
        if isinstance(event, OutputAudio):
            await self._write_native_audio(event, generation)
            return
        if isinstance(event, OutputTranscript):
            await self._project_native_transcript(event, generation)
            return
        if isinstance(event, ResponseCompleted):
            await self._complete_native_provider(event, generation)
            return
        if isinstance(event, Interruption):
            await self._fail_native("unsolicited provider interruption")
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
                    self._host_projection_lifecycle = None
                    self._emit(
                        ControllerLifecycle.QUEUED,
                        provider_event=event,
                        generation=generation,
                        admission_status=result.status,
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
        # Provider tools and all other events are projection-only in this slice.
        self._emit(
            self._lifecycle_events[-1].lifecycle,
            provider_event=event,
            generation=generation,
        )

    async def _fail_native(self, detail: str) -> None:
        owner = asyncio.current_task()
        if self._native_failure is None:
            self._native_failure = detail
            self._native_failure_owner = owner
            self._emit(ControllerLifecycle.NATIVE_FAILED, detail=detail)
            self._emit(ControllerLifecycle.FAILED, detail=detail)
        try:
            await self.close(reason=detail)
        finally:
            if self._native_failure_owner is owner:
                self._native_failure_owner = None

    async def _start_native_playback(
        self, event: ResponseStarted, generation: int
    ) -> None:
        async with self._output_lock:
            state = self._native_output
            invalid = (
                state is None
                or state.transport_generation != generation
                or state.turn_marker != event.turn_id
                or not state.send_accepted
                or state.response_id is not None
                or state.output_format is None
                or event.continuation_of_batch_id is not None
            )
            if not invalid:
                state.response_id = event.response_id
                binding = self._binding
                output_format = state.output_format
                sink = self._output_sink
        if invalid:
            await self._fail_native("invalid or duplicate provider response start")
            return
        assert state is not None
        assert binding is not None and sink is not None and output_format is not None
        try:
            lease = await sink.open_lease(
                binding, event.response_id, event.turn_id, output_format, generation
            )
            lease_id = lease.lease_id
            lease_response_id = lease.response_id
            lease_turn_marker = lease.turn_marker
            lease_generation = lease.transport_generation
            _validate_native_identifier(lease_id, "lease_id")
            valid_lease = (
                lease_response_id == event.response_id
                and lease_turn_marker == event.turn_id
                and type(lease_generation) is int
                and lease_generation == generation
            )
        except BaseException:
            await self._fail_native("native playback lease open failed")
            return
        if not valid_lease:
            try:
                await lease.close()
            finally:
                await self._fail_native("native playback lease identity mismatch")
            return
        async with self._output_lock:
            if self._native_output is not state or self._closing:
                stale = True
            else:
                stale = False
                state.lease = lease
                state.lease_id = lease_id
        if stale:
            await lease.close()
            return
        self._emit(ControllerLifecycle.NATIVE_STARTED, provider_event=event, generation=generation)

    async def _write_native_audio(self, event: OutputAudio, generation: int) -> None:
        async with self._output_lock:
            state = self._native_output
            valid = (
                state is not None
                and state.transport_generation == generation
                and state.response_id == event.response_id
                and state.turn_marker == event.turn_id
                and state.lease is not None
                and not state.provider_completed
                and bool(event.data)
                and state.output_format is not None
                and state.output_format.sample_width_bytes is not None
                and len(event.data) % state.output_format.sample_width_bytes == 0
                and (state.item_id is None or state.item_id == event.item_id)
            )
            if valid and state is not None and state.item_id is None:
                state.item_id = event.item_id
            lease = state.lease if valid and state is not None else None
        if not valid or state is None or lease is None:
            await self._fail_native("invalid native PCM output")
            return
        try:
            await lease.write_pcm(event.data)
        except BaseException:
            await self._fail_native("native PCM write failed")
            return
        async with self._output_lock:
            if self._native_output is not state or self._closing:
                return
            state.bytes_written += len(event.data)
            first = not state.playing
            state.playing = True
        if first:
            self._emit(ControllerLifecycle.NATIVE_PLAYING, provider_event=event, generation=generation)

    async def _project_native_transcript(
        self, event: OutputTranscript, generation: int
    ) -> None:
        async with self._output_lock:
            state = self._native_output
            valid = (
                state is not None
                and state.transport_generation == generation
                and state.response_id == event.response_id
                and state.turn_marker == event.turn_id
                and state.lease is not None
                and type(event.final) is bool
                and (state.item_id is None or state.item_id == event.item_id)
            )
            if valid and state is not None and state.item_id is None:
                state.item_id = event.item_id
        if not valid:
            await self._fail_native("invalid native output transcript")
            return
        self._emit(
            self._lifecycle_events[-1].lifecycle,
            provider_event=event,
            generation=generation,
        )

    async def _complete_native_provider(
        self, event: ResponseCompleted, generation: int
    ) -> None:
        async with self._output_lock:
            state = self._native_output
            valid = (
                state is not None
                and state.transport_generation == generation
                and state.response_id == event.response_id
                and state.turn_marker == event.turn_id
                and state.lease is not None
                and state.bytes_written > 0
                and not state.provider_completed
            )
            if valid and state is not None:
                state.provider_completed = True
        if not valid or state is None:
            await self._fail_native("invalid or duplicate provider completion")
            return
        self._emit(ControllerLifecycle.NATIVE_DRAINING, provider_event=event, generation=generation)
        task = asyncio.create_task(self._drain_native_output(state))
        self._output_drain_task = task
        if task.done() and self._output_drain_task is task:
            self._output_drain_task = None

    async def _drain_native_output(self, state: _NativeOutputState) -> None:
        lease = state.lease
        sink = self._output_sink
        assert lease is not None and sink is not None and state.response_id is not None
        try:
            receipt = await lease.finish_and_wait(self._interrupt_timeout)
            valid = (
                type(receipt) is NativePlaybackReceipt
                and sink.validate_playback_receipt(lease, receipt) is True
                and receipt.lease_id == state.lease_id
                and receipt.response_id == state.response_id
                and receipt.turn_marker == state.turn_marker
                and receipt.transport_generation == state.transport_generation
                and receipt.bytes_written == state.bytes_written
                and receipt.interrupted is False
            )
            if not valid:
                raise RuntimeError("invalid native playback drain receipt")
            await self._close_native_lease(state)
            async with self._output_lock:
                if (
                    self._native_output is not state
                    or state.transport_generation != self._transport_generation
                    or self._closing
                ):
                    return
                self._native_output = None
            self._emit(ControllerLifecycle.NATIVE_DRAINED, generation=state.transport_generation)
        except asyncio.CancelledError:
            raise
        except BaseException:
            await self._fail_native("native playback drain failed")
        finally:
            current = asyncio.current_task()
            if self._output_drain_task is current:
                self._output_drain_task = None

    async def _close_native_lease(self, state: _NativeOutputState) -> None:
        lease = state.lease
        if lease is None or state.lease_closed:
            return
        state.lease_closed = True
        await lease.close()

    async def _fence_native_output(self) -> None:
        async with self._output_lock:
            state = self._native_output
            self._native_output = None
        current = asyncio.current_task()
        failure_owner = self._native_failure_owner
        tasks = [
            task
            for task in (self._output_send_task, self._output_drain_task)
            if task is not None and task is not current and task is not failure_owner
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if state is not None:
            await self._close_native_lease(state)

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
        except BaseException as exc:
            if not self._closing:
                self._emit(ControllerLifecycle.FAILED, detail=f"audio send failed: {exc}")
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
            self._emit(ControllerLifecycle.RECONNECTING, detail="audio rejected while reconnecting")
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
                if RealtimeCapability.EXPLICIT_INTERRUPTION in session.capabilities:
                    await session.interrupt()
                await self._host.interrupt_and_wait(binding, self._interrupt_timeout)
            except BaseException as exc:
                # Fence admission before releasing the barrier. Scheduling close()
                # first would leave one event-loop turn where a final transcript
                # could authorize against an attachment already known to be unsafe.
                if not self._closing:
                    self._closing = True
                    self._emit(ControllerLifecycle.CLOSING, detail="interrupt failed")
                failure = exc
        if failure is not None:
            await self.close(reason="interrupt failed")
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
        if projection.status is HostProjectionStatus.COMPLETED and (
            projection.finalization is None
            or not self._host.validate_finalization(projection.finalization)
        ):
            raise RuntimeError("completed projection requires canonical persistence receipt")
        if (
            projection.status is HostProjectionStatus.COMPLETED
            and self._native_output is not None
        ):
            raise RuntimeError("native output is already reserved or active")
        current = self._host_projection_lifecycle or self._lifecycle_events[-1].lifecycle
        rank = {
            ControllerLifecycle.THINKING: 0,
            ControllerLifecycle.ACTING: 1,
            ControllerLifecycle.SPEAKING: 2,
            ControllerLifecycle.COMPLETED: 3,
            ControllerLifecycle.FAILED: 3,
        }
        projected = ControllerLifecycle(projection.status.value)
        if (
            (current in rank and (rank[projected] < rank[current] or rank[current] == 3))
            or (current not in rank and projected is not ControllerLifecycle.THINKING)
        ):
            raise RuntimeError("host projection is not a monotonic canonical transition")

        native = (
            projected is ControllerLifecycle.COMPLETED
            and self._output_sink is not None
            and self._session is not None
            and RealtimeCapability.EXPLICIT_RESPONSE in self._session.capabilities
        )
        if native:
            finalization = projection.finalization
            assert finalization is not None
            try:
                durable_session_id = finalization.durable_session_id
                assistant_message_id = finalization.assistant_message_id
                turn_marker = finalization.turn_marker
                _validate_native_identifier(durable_session_id, "durable_session_id")
                _validate_native_identifier(turn_marker, "turn_marker")
            except (AttributeError, TypeError, ValueError):
                raise RuntimeError("invalid canonical finalization identity") from None
            if (
                durable_session_id != projection.binding.durable_session_id
                or type(assistant_message_id) is not int
                or assistant_message_id <= 0
            ):
                raise RuntimeError("invalid canonical finalization identity")
            async with self._output_lock:
                if self._native_output is not None:
                    raise RuntimeError("native output is already reserved or active")
                state = _NativeOutputState(
                    durable_session_id,
                    assistant_message_id,
                    turn_marker,
                    self._transport_generation,
                )
                self._native_output = state
            self._host_projection_lifecycle = projected
            self._emit(projected, detail=projection.detail)
            self._emit(ControllerLifecycle.NATIVE_RESERVED)
            task = asyncio.create_task(
                self._claim_and_send_native(state, projection.binding, finalization)
            )
            self._output_send_task = task
            if task.done() and self._output_send_task is task:
                self._output_send_task = None
            await asyncio.shield(task)
            return
        self._host_projection_lifecycle = projected
        self._emit(projected, detail=projection.detail)

    async def _claim_and_send_native(
        self,
        state: _NativeOutputState,
        binding: RealtimeSessionBinding,
        finalization: object,
    ) -> None:
        try:
            request = await self._host.claim_native_response(binding, finalization)
            if (
                type(request) is not RealtimeResponseRequest
                or request.durable_session_id != state.durable_session_id
                or request.assistant_message_id != state.assistant_message_id
                or request.turn_marker != state.turn_marker
                or type(request.output_audio_format) is not RealtimeOutputAudioFormat
                or request.allow_tools is not False
            ):
                raise RuntimeError("canonical native response claim identity mismatch")
            async with self._output_lock:
                if (
                    self._native_output is not state
                    or state.transport_generation != self._transport_generation
                    or self._closing
                ):
                    return
                state.content_digest = request.content_digest
                state.output_format = request.output_audio_format
            session = self._session
            if session is None:
                raise RuntimeError("realtime provider session is unavailable")
            await session.start_response(request)
            async with self._output_lock:
                if self._native_output is state and not self._closing:
                    state.send_accepted = True
        except asyncio.CancelledError:
            if not self._closing and self._native_output is state:
                await self._fail_native("native provider send cancelled")
            raise
        except BaseException:
            await self._fail_native("native provider send failed")
            raise
        finally:
            current = asyncio.current_task()
            if self._output_send_task is current:
                self._output_send_task = None

    async def close(self, *, reason: str = "") -> None:
        async with self._close_lock:
            if self._closed:
                return
            close_task = self._close_task
            if close_task is None or close_task.done():
                close_task = asyncio.create_task(self._finish_close(reason))
                self._close_task = close_task
        await asyncio.shield(close_task)

    async def _finish_close(self, reason: str) -> None:
        """Own cleanup independently of any one caller waiting for close."""
        if self._closed:
            return
        if not self._closing:
            self._closing = True
            self._emit(ControllerLifecycle.CLOSING, detail=reason)
        await self._fence_native_output()
        open_task = self._open_task
        if open_task is not None and open_task is not asyncio.current_task():
            try:
                late_session = await asyncio.shield(open_task)
            except BaseException:
                late_session = None
            if late_session is not None and self._session is None:
                self._session = late_session
        admission = self._admission
        if admission is not None:
            await admission.close()
        close_attachment = getattr(self._host, "close_attachment", None)
        if close_attachment is not None:
            await close_attachment(self._binding)
        current = asyncio.current_task()
        failure_owner = self._native_failure_owner
        tasks = [
            task
            for task in (self._event_task, self._audio_task)
            if task is not None and task is not current and task is not failure_owner
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        session = self._session
        if session is not None:
            await session.close()
        self._closed = True
        self._emit(ControllerLifecycle.CLOSED, detail=reason)


__all__ = [
    "AudioFeedResult",
    "ControllerEvent",
    "ControllerLifecycle",
    "GatewayRealtimeVoiceController",

    "HostProjection",
    "HostProjectionStatus",
    "NativeAudioOutputSink",
    "NativeAudioPlaybackLease",
    "NativePlaybackReceipt",
    "RealtimeControllerHost",
]
