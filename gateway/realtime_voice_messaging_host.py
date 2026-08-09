"""Gateway-owned canonical ingress for Discord realtime final transcripts.

The provider/controller never executes Hermes work.  This host consumes a
host-minted attachment factory, synthesizes a normal ``MessageEvent``, and
requires the installed gateway handler to prove the exact canonical user row
and following assistant row were durably persisted before returning a receipt.
"""

from __future__ import annotations

import asyncio
import uuid
import weakref
from dataclasses import dataclass
from typing import Any

from agent.realtime_voice_admission import RealtimeSessionBinding, RealtimeUtterance
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


class RealtimeVoiceIngressError(PermissionError):
    """Canonical realtime ingress was rejected or could not be proven durable."""


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class RealtimeVoiceFinalizationReceipt:
    durable_session_id: str
    turn_marker: str
    user_message_id: int
    assistant_message_id: int


class _Permit:
    __slots__ = ()


@dataclass(slots=True)
class _PermitRecord:
    binding: RealtimeSessionBinding
    utterance: RealtimeUtterance
    after_message_id: int


@dataclass(slots=True)
class _CanonicalClaim:
    host: "GatewayRealtimeVoiceMessagingHost"
    binding: RealtimeSessionBinding
    utterance: RealtimeUtterance
    after_message_id: int
    turn_marker: str
    preflighted: bool = False
    resolved: bool = False
    receipt: RealtimeVoiceFinalizationReceipt | None = None


_ACCEPTED_TASKS: set[asyncio.Task[Any]] = set()
_CLAIM_ATTR = "_hermes_realtime_voice_canonical_claim"
_MARKER_KEY = "realtime_voice_turn_marker"


def _retain_accepted_task(task: asyncio.Task[Any]) -> None:
    _ACCEPTED_TASKS.add(task)

    def done(completed: asyncio.Task[Any]) -> None:
        _ACCEPTED_TASKS.discard(completed)
        if not completed.cancelled():
            try:
                completed.exception()
            except BaseException:
                pass

    task.add_done_callback(done)


def _sync_db(runner: object) -> object:
    wrapper = getattr(runner, "_session_db", None)
    db = getattr(wrapper, "_db", wrapper)
    if db is None or not callable(getattr(db, "get_messages", None)):
        raise RealtimeVoiceIngressError("canonical SessionDB is unavailable")
    return db


def _max_message_id(db: object, durable_session_id: str) -> int:
    rows = db.get_messages(durable_session_id, include_inactive=True)
    return max(
        (
            row.get("id", 0)
            for row in rows
            if type(row.get("id")) is int and row.get("id", 0) > 0
        ),
        default=0,
    )


class GatewayRealtimeVoiceMessagingHost:
    """Consume-once authorizer and canonical same-session ingress."""

    def __init__(self, factory: object, runner: object) -> None:
        from gateway.realtime_voice_invocation import (
            _validate_realtime_voice_attachment_factory,
        )

        self._factory = factory
        self._runner_ref = weakref.ref(runner)
        self._authority = _validate_realtime_voice_attachment_factory(factory, runner)
        self._permits: dict[_Permit, _PermitRecord] = {}
        self._finalizations: weakref.WeakSet[RealtimeVoiceFinalizationReceipt] = (
            weakref.WeakSet()
        )
        self._lock = asyncio.Lock()

    def _runner(self) -> object:
        runner = self._runner_ref()
        if runner is None:
            raise RealtimeVoiceIngressError("gateway host is no longer available")
        return runner

    def _validate_binding(self, binding: RealtimeSessionBinding) -> None:
        from gateway.realtime_voice_invocation import (
            _validate_realtime_voice_attachment_factory,
        )

        runner = self._runner()
        authority = _validate_realtime_voice_attachment_factory(self._factory, runner)
        expected = RealtimeSessionBinding(
            profile_id=authority.profile or "default",
            routing_key=authority.routing_key,
            runtime_session_id=authority.routing_key,
            durable_session_id=authority.durable_session_id,
            provider_session_id=binding.provider_session_id,
            selection_generation=self._factory_generation(),
        )
        if binding != expected:
            raise RealtimeVoiceIngressError(
                "realtime binding does not match captured host authority"
            )

    def _factory_generation(self) -> int:
        from gateway.realtime_voice_invocation import (
            _record_for_realtime_voice_attachment_factory,
        )

        record = _record_for_realtime_voice_attachment_factory(
            self._factory, self._runner()
        )
        return record.routing_generation

    async def authorize(
        self, binding: RealtimeSessionBinding, utterance: RealtimeUtterance
    ) -> object | None:
        self._validate_binding(binding)
        if (
            utterance.provider_session_id != binding.provider_session_id
            or utterance.text.lstrip().startswith("/")
        ):
            # Provider transcripts are conversational input only.  They cannot
            # acquire slash-command/control authority from their text shape.
            return None
        runner = self._runner()
        is_running = getattr(runner, "_is_session_running", None)
        if callable(is_running) and is_running(binding.routing_key):
            return None
        db = _sync_db(runner)
        after_message_id = _max_message_id(db, binding.durable_session_id)
        permit = _Permit()
        async with self._lock:
            self._permits[permit] = _PermitRecord(binding, utterance, after_message_id)
        return permit

    async def revoke(self, permit: object) -> None:
        async with self._lock:
            self._permits.pop(permit, None)

    async def submit(
        self,
        binding: RealtimeSessionBinding,
        utterance: RealtimeUtterance,
        permit: object,
    ) -> RealtimeVoiceFinalizationReceipt:
        async with self._lock:
            record = self._permits.pop(permit, None)
        if record is None:
            raise RealtimeVoiceIngressError(
                "realtime permit was already consumed or revoked"
            )
        if record.binding != binding or record.utterance is not utterance:
            raise RealtimeVoiceIngressError(
                "realtime permit does not match the exact utterance"
            )
        self._validate_binding(binding)
        runner = self._runner()
        is_running = getattr(runner, "_is_session_running", None)
        if callable(is_running) and is_running(binding.routing_key):
            raise RealtimeVoiceIngressError("canonical realtime route became busy")

        authority = self._authority
        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id=authority.chat_id,
            chat_type=authority.chat_type,
            user_id=authority.principal_id,
            thread_id=authority.thread_id,
            scope_id=authority.scope_id,
            profile=authority.profile,
            is_bot=False,
        )
        event = MessageEvent(
            text=utterance.text,
            message_type=MessageType.TEXT,
            source=source,
            user_id=authority.principal_id,
            metadata={},
        )
        claim = _CanonicalClaim(
            host=self,
            binding=binding,
            utterance=utterance,
            after_message_id=record.after_message_id,
            turn_marker=uuid.uuid4().hex,
        )
        setattr(event, _CLAIM_ATTR, claim)
        task = asyncio.create_task(runner._handle_message(event))
        _retain_accepted_task(task)
        await asyncio.shield(task)
        if claim.receipt is None:
            raise RealtimeVoiceIngressError(
                "canonical handler returned without a durable realtime receipt"
            )
        return claim.receipt

    async def interrupt_and_wait(
        self, binding: RealtimeSessionBinding, timeout: float
    ) -> None:
        self._validate_binding(binding)
        # The first Discord canary is idle-only and never owns an interruptible
        # canonical turn.  Once accepted, work belongs to the gateway.
        return None

    async def close_attachment(self, binding: RealtimeSessionBinding | None) -> None:
        async with self._lock:
            self._permits.clear()

    def validate_finalization(self, receipt: object) -> bool:
        return (
            type(receipt) is RealtimeVoiceFinalizationReceipt
            and receipt in self._finalizations
        )


def _create_messaging_host(
    factory: object, runner: object
) -> GatewayRealtimeVoiceMessagingHost:
    return GatewayRealtimeVoiceMessagingHost(factory, runner)


class GatewayRealtimeVoiceAttachment:
    """Narrow lifecycle/audio facade over one host-owned controller."""

    __slots__ = ("binding", "_controller", "_operator_user_id")

    def __init__(
        self,
        binding: RealtimeSessionBinding,
        controller: object,
        *,
        operator_user_id: str,
    ) -> None:
        if (
            type(operator_user_id) is not str
            or not operator_user_id.isascii()
            or not operator_user_id.isdecimal()
            or operator_user_id.startswith("0")
        ):
            raise ValueError("operator_user_id must be a canonical positive Discord ID")
        self.binding = binding
        self._controller = controller
        self._operator_user_id = operator_user_id

    @property
    def lifecycle_events(self) -> tuple[object, ...]:
        return self._controller.lifecycle_events

    @property
    def operator_user_id(self) -> int:
        """The exact immutable native Discord principal authorized by the host."""

        return int(self._operator_user_id)

    def feed_audio(
        self,
        data: bytes | bytearray | memoryview,
        *,
        speaker_user_id: object,
        mime_type: str | None = None,
    ):
        from gateway.realtime_voice_controller import AudioFeedResult

        if (
            type(speaker_user_id) is not int
            or speaker_user_id <= 0
            or str(speaker_user_id) != self._operator_user_id
        ):
            return AudioFeedResult.UNAUTHORIZED
        return self._controller.feed_audio(data, mime_type=mime_type)

    def feed_synthesized_silence(self, data: object):
        """Feed only exact, aligned, all-zero PCM without speaker attribution."""

        from gateway.realtime_voice_controller import AudioFeedResult

        if type(data) is not bytes or not data or len(data) % 2 != 0 or any(data):
            return AudioFeedResult.UNAUTHORIZED
        return self._controller.feed_audio(data, mime_type="audio/pcm")

    async def interrupt(self) -> None:
        await self._controller.interrupt()

    async def close(self) -> None:
        await self._controller.close(reason="attachment closed")


async def _open_attachment(
    factory: object,
    runner: object,
    provider_name: str,
    setup: object,
    *,
    provider_session_id: str,
    required_capabilities: object = frozenset(),
) -> GatewayRealtimeVoiceAttachment:
    from agent.realtime_voice_provider import RealtimeCapability, RealtimeVoiceSetup
    from gateway.realtime_voice_controller import GatewayRealtimeVoiceController
    from gateway.realtime_voice_invocation import (
        _record_for_realtime_voice_attachment_factory,
        _validate_realtime_voice_attachment_factory,
    )

    if (
        type(provider_name) is not str
        or not provider_name
        or provider_name.strip() != provider_name
    ):
        raise ValueError("provider_name must be a nonblank normalized string")
    if (
        type(provider_session_id) is not str
        or not provider_session_id
        or provider_session_id.strip() != provider_session_id
    ):
        raise ValueError("provider_session_id must be a nonblank normalized string")
    if type(setup) is not RealtimeVoiceSetup:
        raise TypeError("setup must be an exact RealtimeVoiceSetup")
    if type(required_capabilities) is not frozenset or any(
        type(capability) is not RealtimeCapability
        for capability in required_capabilities
    ):
        raise TypeError(
            "required_capabilities must be a frozenset of exact RealtimeCapability values"
        )
    authority = _validate_realtime_voice_attachment_factory(factory, runner)
    record = _record_for_realtime_voice_attachment_factory(factory, runner)
    binding = RealtimeSessionBinding(
        profile_id=authority.profile or "default",
        routing_key=authority.routing_key,
        runtime_session_id=authority.routing_key,
        durable_session_id=authority.durable_session_id,
        provider_session_id=provider_session_id,
        selection_generation=record.routing_generation,
    )
    host = GatewayRealtimeVoiceMessagingHost(factory, runner)
    controller = GatewayRealtimeVoiceController(host)
    await controller.open(
        provider_name,
        setup,
        binding,
        required_capabilities=required_capabilities,
    )
    return GatewayRealtimeVoiceAttachment(
        binding,
        controller,
        operator_user_id=authority.principal_id,
    )


def _claim_for(runner: object, event: object) -> _CanonicalClaim | None:
    claim = getattr(event, _CLAIM_ATTR, None)
    if claim is None:
        return None
    if type(claim) is not _CanonicalClaim or claim.host._runner_ref() is not runner:
        raise RealtimeVoiceIngressError("invalid canonical realtime claim")
    claim.host._validate_binding(claim.binding)
    return claim


def _preflight_realtime_voice_event(
    runner: object, event: object, routing_key: str
) -> bool:
    """Fail before ordinary busy handling can queue, steer, or interrupt."""

    claim = _claim_for(runner, event)
    if claim is None:
        return False
    if claim.binding.routing_key != routing_key:
        raise RealtimeVoiceIngressError("realtime route changed before canonical claim")
    is_running = getattr(runner, "_is_session_running", None)
    if callable(is_running) and is_running(routing_key):
        raise RealtimeVoiceIngressError("canonical realtime route is busy")
    claim.preflighted = True
    return True


def _validate_realtime_voice_event_after_resolution(
    runner: object, event: object, session_entry: object
) -> bool:
    claim = _claim_for(runner, event)
    if claim is None:
        return False
    if not claim.preflighted:
        raise RealtimeVoiceIngressError("realtime event bypassed canonical preflight")
    if (
        getattr(session_entry, "session_key", None) != claim.binding.routing_key
        or getattr(session_entry, "session_id", None)
        != claim.binding.durable_session_id
    ):
        raise RealtimeVoiceIngressError(
            "realtime durable session changed before turn lease"
        )
    claim.resolved = True
    return True


async def _finalize_realtime_voice_event(
    runner: object, event: object, durable_session_id: str | None
) -> RealtimeVoiceFinalizationReceipt | None:
    """Stamp and read back the exact canonical exchange while its lease is held."""

    claim = _claim_for(runner, event)
    if claim is None:
        return None
    if durable_session_id is None:
        durable_session_id = claim.binding.durable_session_id
    if not claim.resolved:
        raise RealtimeVoiceIngressError(
            "canonical realtime turn did not reach the resolved-session lease boundary"
        )
    if durable_session_id != claim.binding.durable_session_id:
        raise RealtimeVoiceIngressError(
            "canonical turn finalized in a different durable session"
        )
    db = _sync_db(runner)
    rows = [
        row
        for row in db.get_messages(durable_session_id, include_inactive=True)
        if type(row.get("id")) is int and row["id"] > claim.after_message_id
    ]
    user_rows = [row for row in rows if row.get("role") == "user"]
    if len(user_rows) != 1:
        raise RealtimeVoiceIngressError(
            "exact canonical realtime user row was not found"
        )
    user = user_rows[0]
    if type(user.get("content")) is not str or user["content"] != claim.utterance.text:
        raise RealtimeVoiceIngressError(
            "canonical realtime user row does not match the accepted utterance"
        )
    assistant = next(
        (
            row
            for row in rows
            if row["id"] > user["id"] and row.get("role") == "assistant"
        ),
        None,
    )
    if assistant is None:
        raise RealtimeVoiceIngressError(
            "canonical assistant row was not durably persisted"
        )
    stamped = db.set_message_display_kind(
        durable_session_id,
        user["id"],
        display_kind="realtime_voice_turn",
        display_metadata={_MARKER_KEY: claim.turn_marker},
    )
    if stamped is not True:
        raise RealtimeVoiceIngressError(
            "canonical realtime marker could not be persisted"
        )
    reread = db.get_messages(durable_session_id, include_inactive=True)
    marked = next(
        (
            row
            for row in reread
            if row.get("id") == user["id"]
            and isinstance(row.get("display_metadata"), dict)
            and row["display_metadata"].get(_MARKER_KEY) == claim.turn_marker
        ),
        None,
    )
    if marked is None:
        raise RealtimeVoiceIngressError("canonical realtime marker read-back failed")
    receipt = RealtimeVoiceFinalizationReceipt(
        durable_session_id=durable_session_id,
        turn_marker=claim.turn_marker,
        user_message_id=user["id"],
        assistant_message_id=assistant["id"],
    )
    claim.receipt = receipt
    claim.host._finalizations.add(receipt)
    return receipt


__all__ = [
    "GatewayRealtimeVoiceMessagingHost",
    "RealtimeVoiceFinalizationReceipt",
    "RealtimeVoiceIngressError",
]
