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
    RealtimeCapability,
    RealtimeVoiceEvent,
    RealtimeVoiceSession,
    RealtimeVoiceSetup,
    SessionClosed,
    SessionFailure,
    SessionReady,
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
    async def authorize(self, binding: RealtimeSessionBinding, utterance: Any) -> Any: ...

    async def revoke(self, permit: Any) -> None: ...

    async def submit(
        self, binding: RealtimeSessionBinding, utterance: Any, permit: Any
    ) -> Any: ...

    async def interrupt_and_wait(
        self, binding: RealtimeSessionBinding, timeout: float
    ) -> None: ...

    def validate_finalization(self, receipt: object) -> bool: ...


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
            (current in rank and (rank[projected] < rank[current] or rank[current] == 3))
            or (current not in rank and projected is not ControllerLifecycle.THINKING)
        ):
            raise RuntimeError("host projection is not a monotonic canonical transition")
        self._emit(projected, detail=projection.detail)

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
    "RealtimeControllerHost",
]
