"""Host-authorized admission for provider-neutral final voice transcripts."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Generic, Protocol, TypeVar

from agent.realtime_voice_provider import (
    MAX_IDENTIFIER_LENGTH,
    InputTranscript,
    RealtimeVoiceEvent,
    TranscriptProvenance,
    TranscriptRole,
)

PermitT = TypeVar("PermitT")
ReceiptT = TypeVar("ReceiptT")


def _validate_identifier(value: object, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_IDENTIFIER_LENGTH
    ):
        raise ValueError(
            f"{field_name} must be a nonblank, trimmed identifier no longer than "
            f"{MAX_IDENTIFIER_LENGTH} characters"
        )


def _validate_positive_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be a positive integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class RealtimeSessionBinding:
    profile_id: str
    routing_key: str
    runtime_session_id: str
    durable_session_id: str
    provider_session_id: str
    selection_generation: int

    def __post_init__(self) -> None:
        for field_name in (
            "profile_id",
            "routing_key",
            "runtime_session_id",
            "durable_session_id",
            "provider_session_id",
        ):
            _validate_identifier(getattr(self, field_name), field_name)
        _validate_positive_int(self.selection_generation, "selection_generation")


@dataclass(frozen=True, slots=True)
class RealtimeUtterance:
    provider_session_id: str
    provider_turn_id: str
    item_id: str
    text: str
    received_at: float

    def __post_init__(self) -> None:
        for field_name in (
            "provider_session_id",
            "provider_turn_id",
            "item_id",
        ):
            _validate_identifier(getattr(self, field_name), field_name)
        if not isinstance(self.text, str):
            raise TypeError("text must be a nonblank, trimmed string")
        if not self.text or self.text != self.text.strip():
            raise ValueError("text must be a nonblank, trimmed string")
        if (
            isinstance(self.received_at, bool)
            or not isinstance(self.received_at, (int, float))
        ):
            raise TypeError("received_at must be a finite number")
        if not math.isfinite(self.received_at):
            raise ValueError("received_at must be a finite number")


class RealtimeInputAuthorizer(Protocol[PermitT]):
    async def authorize(
        self,
        binding: RealtimeSessionBinding,
        utterance: RealtimeUtterance,
    ) -> PermitT | None: ...

    async def revoke(self, permit: PermitT) -> None: ...


class SameSessionTurnIngress(Protocol[PermitT, ReceiptT]):
    async def submit(
        self,
        binding: RealtimeSessionBinding,
        utterance: RealtimeUtterance,
        permit: PermitT,
    ) -> ReceiptT: ...


class AdmissionStatus(StrEnum):
    IGNORED_PARTIAL = "ignored_partial"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class AdmissionResult(Generic[ReceiptT]):
    status: AdmissionStatus
    receipt: ReceiptT | None = None


class FinalTranscriptAdmission(Generic[PermitT, ReceiptT]):
    def __init__(
        self,
        binding: RealtimeSessionBinding,
        authorizer: RealtimeInputAuthorizer[PermitT],
        ingress: SameSessionTurnIngress[PermitT, ReceiptT],
        *,
        replay_capacity: int = 1024,
        max_transcript_chars: int = 32_768,
        clock: Callable[[], float],
    ) -> None:
        _validate_positive_int(replay_capacity, "replay_capacity")
        _validate_positive_int(max_transcript_chars, "max_transcript_chars")
        self._binding = binding
        self._authorizer = authorizer
        self._ingress = ingress
        self._replay_capacity = replay_capacity
        self._max_transcript_chars = max_transcript_chars
        self._clock = clock
        self._reserved_keys: set[tuple[str, str, str]] = set()
        self._capacity_exhausted = False
        self._closed = False
        self._pending_permits: dict[tuple[str, str, str], PermitT] = {}
        self._pending_revocations: tuple[PermitT, ...] = ()
        self._close_lock = asyncio.Lock()

    async def _revoke_permits(self, permits: tuple[PermitT, ...]) -> None:
        if not permits:
            return

        async def revoke_all() -> None:
            await asyncio.gather(
                *(self._authorizer.revoke(permit) for permit in permits)
            )

        task = asyncio.create_task(revoke_all())
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.done():
                    raise RuntimeError("permit revocation was cancelled") from None
                cancelled = True
                continue
        if cancelled:
            raise asyncio.CancelledError

    async def _authorize_tracked(
        self, utterance: RealtimeUtterance
    ) -> tuple[PermitT | None, bool]:
        task = asyncio.create_task(
            self._authorizer.authorize(self._binding, utterance)
        )
        cancelled = False
        while True:
            try:
                permit = await asyncio.shield(task)
                return permit, cancelled
            except asyncio.CancelledError:
                if task.done():
                    if task.cancelled():
                        raise
                    return task.result(), True
                cancelled = True

    async def _revoke_unsubmitted(
        self,
        key: tuple[str, str, str],
        permit: PermitT,
    ) -> None:
        try:
            await self._revoke_permits((permit,))
        except BaseException:
            self._pending_permits[key] = permit
            raise

    async def close(self) -> None:
        async with self._close_lock:
            if not self._closed:
                self._closed = True
            if self._pending_permits:
                self._pending_revocations += tuple(self._pending_permits.values())
                self._pending_permits.clear()
            if not self._pending_revocations:
                return
            permits = self._pending_revocations
            try:
                await self._revoke_permits(permits)
            except asyncio.CancelledError:
                self._pending_revocations = ()
                raise
            self._pending_revocations = ()

    async def admit(
        self, event: RealtimeVoiceEvent
    ) -> AdmissionResult[ReceiptT]:
        if self._closed:
            return AdmissionResult(AdmissionStatus.CLOSED)
        if not isinstance(event, InputTranscript):
            return AdmissionResult(AdmissionStatus.REJECTED)
        if event.final is False:
            return AdmissionResult(AdmissionStatus.IGNORED_PARTIAL)

        key = (
            self._binding.provider_session_id,
            event.turn_id,
            event.item_id,
        )
        if key in self._reserved_keys:
            return AdmissionResult(AdmissionStatus.DUPLICATE)
        if self._capacity_exhausted:
            return AdmissionResult(AdmissionStatus.CAPACITY_EXHAUSTED)
        if len(self._reserved_keys) >= self._replay_capacity:
            self._capacity_exhausted = True
            return AdmissionResult(AdmissionStatus.CAPACITY_EXHAUSTED)
        self._reserved_keys.add(key)

        if event.final is not True:
            return AdmissionResult(AdmissionStatus.REJECTED)

        if (
            event.role is not TranscriptRole.OPERATOR
            or event.provenance is not TranscriptProvenance.OPERATOR_INPUT
        ):
            return AdmissionResult(AdmissionStatus.REJECTED)

        text = event.text
        if not isinstance(text, str):
            return AdmissionResult(AdmissionStatus.REJECTED)
        text = text.strip()
        if not text or len(text) > self._max_transcript_chars:
            return AdmissionResult(AdmissionStatus.REJECTED)

        utterance = RealtimeUtterance(
            provider_session_id=self._binding.provider_session_id,
            provider_turn_id=event.turn_id,
            item_id=event.item_id,
            text=text,
            received_at=self._clock(),
        )
        permit, authorization_cancelled = await self._authorize_tracked(utterance)
        if authorization_cancelled:
            if permit is not None:
                await self._revoke_unsubmitted(key, permit)
            raise asyncio.CancelledError
        if self._closed:
            if permit is not None:
                await self._revoke_unsubmitted(key, permit)
            return AdmissionResult(AdmissionStatus.CLOSED)
        if permit is None:
            return AdmissionResult(AdmissionStatus.REJECTED)
        self._pending_permits[key] = permit
        try:
            receipt = await self._ingress.submit(self._binding, utterance, permit)
        except BaseException:
            owned_permit = self._pending_permits.get(key)
            if owned_permit is permit:
                await self._revoke_permits((permit,))
                self._pending_permits.pop(key, None)
            raise
        self._pending_permits.pop(key, None)
        return AdmissionResult(AdmissionStatus.SUBMITTED, receipt)


__all__ = [
    "AdmissionResult",
    "AdmissionStatus",
    "FinalTranscriptAdmission",
    "RealtimeInputAuthorizer",
    "RealtimeSessionBinding",
    "RealtimeUtterance",
    "SameSessionTurnIngress",
]
