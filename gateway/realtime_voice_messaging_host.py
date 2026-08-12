"""Gateway-owned canonical ingress for Discord realtime final transcripts.

The provider/controller never executes Hermes work.  This host consumes a
host-minted attachment factory, synthesizes a normal ``MessageEvent``, and
requires the installed gateway handler to prove the exact canonical user row
and following assistant row were durably persisted before returning a receipt.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import inspect
import uuid
import weakref
from dataclasses import dataclass
from typing import Any

from agent.realtime_voice_admission import RealtimeSessionBinding, RealtimeUtterance
from agent.realtime_voice_provider import (
    MAX_CANONICAL_RESPONSE_TEXT_BYTES,
    RealtimeOutputAudioFormat,
)
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


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class NativeRealtimeOutputReservation:
    durable_session_id: str
    assistant_message_id: int
    turn_marker: str
    content_digest: str
    output_audio_format: RealtimeOutputAudioFormat
    selection_generation: int
    guild_id: int
    connection_generation: int
    mixer_generation: int


@dataclass(frozen=True, slots=True, eq=False)
class NativeRealtimeOutputRequest:
    """Immediate text handoff; only its compact reservation is host-retained."""

    reservation: NativeRealtimeOutputReservation
    canonical_text: str

    @property
    def content_digest(self) -> str:
        return self.reservation.content_digest

    @property
    def output_audio_format(self) -> RealtimeOutputAudioFormat:
        return self.reservation.output_audio_format


@dataclass(slots=True)
class _AcquisitionRecord:
    reservation: NativeRealtimeOutputReservation
    transport_generation: int
    adapter: object
    task: asyncio.Task[object] | None = None
    lease: object | None = None
    failure: BaseException | None = None


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
    captured_entry: object
    preflighted: bool = False
    slot_claimed: bool = False
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


def _is_terminal_assistant_row(row: object) -> bool:
    if type(row) is not dict or row.get("role") != "assistant":
        return False
    tool_calls = row.get("tool_calls")
    return type(row.get("content")) is str and (
        tool_calls is None or (type(tool_calls) is list and not tool_calls)
    )


class GatewayRealtimeVoiceMessagingHost:
    """Consume-once authorizer and canonical same-session ingress."""

    _LEDGER_CAPACITY = 1024

    def __init__(
        self,
        factory: object,
        runner: object,
        *,
        output_audio_format: RealtimeOutputAudioFormat | None = None,
    ) -> None:
        from gateway.realtime_voice_invocation import (
            _validate_realtime_voice_attachment_factory,
        )

        self._factory = factory
        self._runner_ref = weakref.ref(runner)
        self._authority = _validate_realtime_voice_attachment_factory(factory, runner)
        self._permits: dict[_Permit, _PermitRecord] = {}
        self._finalizations: set[RealtimeVoiceFinalizationReceipt] = set()
        self._output_audio_format = output_audio_format
        self._consumed_finalizations: set[RealtimeVoiceFinalizationReceipt] = set()
        self._reservations: set[NativeRealtimeOutputReservation] = set()
        self._acquisitions: dict[NativeRealtimeOutputReservation, _AcquisitionRecord] = {}
        self._closed = False
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
            if self._closed:
                return None
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
            captured_entry=self._captured_entry(),
        )
        setattr(event, _CLAIM_ATTR, claim)
        from gateway.realtime_voice_invocation import (
            _record_for_realtime_voice_attachment_factory,
        )

        adapter = _record_for_realtime_voice_attachment_factory(
            self._factory, runner
        ).adapter_ref()
        process_attached = getattr(adapter, "_process_attached_message", None)
        if not callable(process_attached):
            raise RealtimeVoiceIngressError(
                "captured Discord adapter has no canonical attached delivery path"
            )
        task = asyncio.create_task(process_attached(event, binding.routing_key))
        _retain_accepted_task(task)
        adapter_completion = await asyncio.shield(task)
        if adapter_completion is not True:
            raise RealtimeVoiceIngressError(
                "canonical Discord adapter returned no completion receipt"
            )
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
            self._closed = True
            self._permits.clear()
            tasks = tuple(
                record.task
                for record in self._acquisitions.values()
                if record.task is not None and not record.task.done()
            )
        for task in tasks:
            try:
                await asyncio.shield(task)
            except BaseException:
                pass

    def _native_adapter_state(self) -> tuple[object, int, int, int]:
        from gateway.realtime_voice_invocation import (
            _record_for_realtime_voice_attachment_factory,
        )

        record = _record_for_realtime_voice_attachment_factory(
            self._factory, self._runner()
        )
        adapter = record.adapter_ref()
        if adapter is None or self._runner().adapters.get(Platform.DISCORD) is not adapter:
            raise RealtimeVoiceIngressError("captured Discord adapter is no longer current")
        scope_id = self._authority.scope_id
        if (
            type(scope_id) is not str
            or not scope_id.isascii()
            or not scope_id.isdecimal()
            or scope_id.startswith("0")
        ):
            raise RealtimeVoiceIngressError("Discord guild ID is not canonical")
        guild_id = int(scope_id)
        clients = getattr(adapter, "_voice_clients", None)
        connection_generations = getattr(adapter, "_voice_connection_generations", None)
        mixer_generations = getattr(adapter, "_voice_mixer_generations", None)
        mixers = getattr(adapter, "_voice_mixers", None)
        if not all(type(state) is dict for state in (
            clients, connection_generations, mixer_generations, mixers
        )):
            raise RealtimeVoiceIngressError("Discord native playback state is unavailable")
        client = clients.get(guild_id)
        if client is None or not callable(getattr(client, "is_connected", None)) or not client.is_connected():
            raise RealtimeVoiceIngressError("Discord voice client is not connected")
        connection_generation = connection_generations.get(guild_id)
        mixer_generation = mixer_generations.get(guild_id)
        if (
            type(connection_generation) is not int
            or connection_generation <= 0
            or type(mixer_generation) is not int
            or mixer_generation <= 0
        ):
            raise RealtimeVoiceIngressError("Discord voice generations are invalid")
        mixer = mixers.get(guild_id)
        if mixer is not None:
            owner_is = getattr(adapter, "_voice_mixer_owner_is", None)
            if not callable(owner_is) or not owner_is(
                guild_id, client, mixer, mixer_generation
            ):
                raise RealtimeVoiceIngressError("Discord voice mixer ownership is stale")
        return adapter, guild_id, connection_generation, mixer_generation

    async def reserve_native_output(
        self,
        binding: RealtimeSessionBinding,
        receipt: object,
    ) -> NativeRealtimeOutputRequest:
        self._validate_binding(binding)
        if type(receipt) is not RealtimeVoiceFinalizationReceipt:
            raise RealtimeVoiceIngressError("finalization receipt was not minted by this host")
        self._validate_native_identifier(receipt.durable_session_id, "durable_session_id")
        self._validate_native_identifier(receipt.turn_marker, "turn_marker")
        if (
            type(receipt.user_message_id) is not int
            or receipt.user_message_id <= 0
            or type(receipt.assistant_message_id) is not int
            or receipt.assistant_message_id <= receipt.user_message_id
        ):
            raise RealtimeVoiceIngressError("finalization receipt row IDs are invalid")
        async with self._lock:
            self._validate_binding(binding)
            if self._closed:
                raise RealtimeVoiceIngressError("native output host is closed")
            if receipt in self._consumed_finalizations:
                raise RealtimeVoiceIngressError("finalization receipt was already consumed")
            if (
                receipt not in self._finalizations
            ):
                raise RealtimeVoiceIngressError("finalization receipt was not minted by this host")
            if len(self._consumed_finalizations) >= self._LEDGER_CAPACITY:
                raise RealtimeVoiceIngressError("native output reservation capacity exhausted")
            self._consumed_finalizations.add(receipt)

        output_format = self._output_audio_format
        if (
            type(output_format) is not RealtimeOutputAudioFormat
            or output_format.mime_type != "audio/pcm"
            or output_format.sample_encoding != "pcm_s16le"
            or output_format.sample_width_bytes != 2
            or output_format.sample_rate_hz != 24000
            or output_format.channels != 1
            or output_format.endianness != "little"
        ):
            raise RealtimeVoiceIngressError("native output audio format is unavailable")
        rows = _sync_db(self._runner()).get_messages(
            receipt.durable_session_id, include_inactive=True
        )
        if receipt.durable_session_id != binding.durable_session_id:
            raise RealtimeVoiceIngressError("finalization durable session changed")
        users = [
            row for row in rows
            if (
                type(row) is dict
                and type(row.get("id")) is int
                and row["id"] == receipt.user_message_id
            )
        ]
        assistants = [
            row for row in rows
            if (
                type(row) is dict
                and type(row.get("id")) is int
                and row["id"] == receipt.assistant_message_id
            )
        ]
        if len(users) != 1 or len(assistants) != 1:
            raise RealtimeVoiceIngressError("exact durable finalization rows were not found")
        user, assistant = users[0], assistants[0]
        metadata = user.get("display_metadata")
        if (
            user.get("role") != "user"
            or type(metadata) is not dict
            or metadata.get(_MARKER_KEY) != receipt.turn_marker
        ):
            raise RealtimeVoiceIngressError("canonical realtime marker read-back changed")
        tool_calls = assistant.get("tool_calls")
        text = assistant.get("content")
        if (
            assistant.get("role") != "assistant"
            or type(text) is not str
            or len(text) > MAX_CANONICAL_RESPONSE_TEXT_BYTES
            or not text.strip()
            or not (tool_calls is None or (type(tool_calls) is list and not tool_calls))
        ):
            raise RealtimeVoiceIngressError("canonical assistant row is malformed")
        try:
            canonical_bytes = text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise RealtimeVoiceIngressError("canonical assistant text is not UTF-8") from exc
        if len(canonical_bytes) > MAX_CANONICAL_RESPONSE_TEXT_BYTES:
            raise RealtimeVoiceIngressError("canonical assistant text exceeds byte limit")
        adapter, guild_id, connection_generation, mixer_generation = (
            self._native_adapter_state()
        )
        reservation = NativeRealtimeOutputReservation(
            receipt.durable_session_id,
            receipt.assistant_message_id,
            receipt.turn_marker,
            hashlib.sha256(canonical_bytes).hexdigest(),
            output_format,
            binding.selection_generation,
            guild_id,
            connection_generation,
            mixer_generation,
        )
        async with self._lock:
            if self._closed:
                raise RealtimeVoiceIngressError("native output host is closed")
            current_adapter, current_guild, current_connection, current_mixer = (
                self._native_adapter_state()
            )
            if (
                current_adapter is not adapter
                or current_guild != guild_id
                or current_connection != connection_generation
                or current_mixer != mixer_generation
            ):
                raise RealtimeVoiceIngressError("native playback adapter authority changed")
            if len(self._reservations) >= self._LEDGER_CAPACITY:
                raise RealtimeVoiceIngressError("native output reservation capacity exhausted")
            self._reservations.add(reservation)
        return NativeRealtimeOutputRequest(reservation, text)

    @staticmethod
    def _validate_native_identifier(value: object, name: str) -> str:
        if type(value) is not str or not value or len(value) > 256:
            raise RealtimeVoiceIngressError(f"{name} must be a bounded exact identifier")
        if value.strip() != value:
            raise RealtimeVoiceIngressError(f"{name} must be a bounded exact identifier")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise RealtimeVoiceIngressError(
                f"{name} must be a bounded exact identifier"
            ) from exc
        if len(encoded) > 256:
            raise RealtimeVoiceIngressError(f"{name} must be a bounded exact identifier")
        return value

    async def acquire_native_playback(
        self,
        reservation: object,
        *,
        lease_id: object,
        response_id: object,
        transport_generation: object,
    ) -> object:
        lease_id = self._validate_native_identifier(lease_id, "lease_id")
        response_id = self._validate_native_identifier(response_id, "response_id")
        if type(reservation) is NativeRealtimeOutputRequest:
            reservation = reservation.reservation
        if type(reservation) is not NativeRealtimeOutputReservation:
            raise RealtimeVoiceIngressError("native output reservation is forged or stale")
        if type(transport_generation) is not int or transport_generation <= 0:
            raise RealtimeVoiceIngressError("native playback transport generation is invalid")
        async with self._lock:
            if self._closed or reservation not in self._reservations:
                raise RealtimeVoiceIngressError("native output reservation is forged or stale")
            if reservation in self._acquisitions:
                raise RealtimeVoiceIngressError("native output reservation was already acquired")
            if len(self._acquisitions) >= self._LEDGER_CAPACITY:
                raise RealtimeVoiceIngressError("native playback acquisition capacity exhausted")
            adapter, guild_id, connection_generation, mixer_generation = (
                self._native_adapter_state()
            )
            if (
                guild_id != reservation.guild_id
                or connection_generation != reservation.connection_generation
                or mixer_generation != reservation.mixer_generation
            ):
                raise RealtimeVoiceIngressError("native playback adapter authority changed")
            record = _AcquisitionRecord(reservation, transport_generation, adapter)
            self._acquisitions[reservation] = record

            async def acquire_owned() -> object:
                try:
                    lease = await adapter.acquire_native_playback_lease(
                        guild_id,
                        lease_id,
                        response_id,
                        reservation.turn_marker,
                        connection_generation,
                        mixer_generation,
                        input_format="pcm_s16le",
                        sample_rate=24000,
                        channels=1,
                    )
                    record.lease = lease
                    try:
                        current_adapter, current_guild, current_connection, current_mixer = (
                            self._native_adapter_state()
                        )
                        stale = (
                            self._closed
                            or self._acquisitions.get(reservation) is not record
                            or current_adapter is not adapter
                            or current_guild != guild_id
                            or current_connection != connection_generation
                            or current_mixer != mixer_generation
                        )
                    except BaseException:
                        stale = True
                    if stale:
                        close = getattr(lease, "close", None)
                        if callable(close):
                            result = close()
                            if inspect.isawaitable(result):
                                await result
                        raise RealtimeVoiceIngressError(
                            "native playback authority changed during acquisition"
                        )
                    return lease
                except BaseException as exc:
                    record.failure = exc
                    raise

            task = asyncio.create_task(acquire_owned())
            record.task = task
            _retain_accepted_task(task)
        return await asyncio.shield(task)

    def validate_native_playback_receipt(
        self, reservation: object, lease: object, receipt: object
    ) -> object:
        if type(reservation) is NativeRealtimeOutputRequest:
            reservation = reservation.reservation
        if type(reservation) is not NativeRealtimeOutputReservation:
            raise RealtimeVoiceIngressError("native output reservation is forged or stale")
        record = self._acquisitions.get(reservation)
        if (
            self._closed
            or reservation not in self._reservations
            or type(record) is not _AcquisitionRecord
            or record.reservation is not reservation
            or record.lease is not lease
            or type(record.transport_generation) is not int
            or record.transport_generation <= 0
        ):
            raise RealtimeVoiceIngressError("native output reservation is forged or stale")
        adapter, guild_id, connection_generation, mixer_generation = (
            self._native_adapter_state()
        )
        if (
            adapter is not record.adapter
            or guild_id != reservation.guild_id
            or connection_generation != reservation.connection_generation
            or mixer_generation != reservation.mixer_generation
        ):
            raise RealtimeVoiceIngressError("native playback adapter authority changed")
        validator = getattr(adapter, "validate_native_playback_receipt", None)
        if not callable(validator):
            raise RealtimeVoiceIngressError("native playback receipt validator unavailable")
        return validator(guild_id, lease, receipt)

    def validate_finalization(self, receipt: object) -> bool:
        return (
            type(receipt) is RealtimeVoiceFinalizationReceipt
            and receipt in self._finalizations
        )

    def _captured_entry(self) -> object:
        from gateway.realtime_voice_invocation import (
            _record_for_realtime_voice_attachment_factory,
        )

        entry = _record_for_realtime_voice_attachment_factory(
            self._factory, self._runner()
        ).entry_ref()
        if entry is None:
            raise RealtimeVoiceIngressError("captured session entry is unavailable")
        return entry


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
    if setup.tools:
        raise RealtimeVoiceIngressError(
            "provider tools are forbidden for the canonical messaging host"
        )
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
    host = GatewayRealtimeVoiceMessagingHost(
        factory, runner, output_audio_format=setup.output_audio
    )
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
    claim = inspect.getattr_static(event, _CLAIM_ATTR, None)
    if claim is None:
        return None
    if type(claim) is not _CanonicalClaim or claim.host._runner_ref() is not runner:
        raise RealtimeVoiceIngressError("invalid canonical realtime claim")
    claim.host._validate_binding(claim.binding)
    return claim


def _is_native_realtime_event(runner: object, event: object) -> bool:
    return _claim_for(runner, event) is not None


def _rewrite_realtime_voice_event(runner: object, event: object, text: str) -> object:
    """Preserve only the exact host claim across a legitimate dataclass rewrite."""

    claim = _claim_for(runner, event)
    replacement = dataclasses.replace(event, text=text)
    if claim is None:
        return replacement
    replacement_claim = getattr(replacement, _CLAIM_ATTR, None)
    if replacement_claim is not None and replacement_claim is not claim:
        raise RealtimeVoiceIngressError(
            "rewritten realtime event substituted its canonical claim"
        )
    if replacement_claim is None:
        setattr(replacement, _CLAIM_ATTR, claim)
    if _claim_for(runner, replacement) is not claim:
        raise RealtimeVoiceIngressError(
            "rewritten realtime event lost its exact canonical claim"
        )
    return replacement


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


def _prepare_realtime_voice_slot_claim(
    runner: object, event: object, routing_key: str
) -> _CanonicalClaim | None:
    """Validate idle-only authority at the actual routing-slot boundary."""

    claim = _claim_for(runner, event)
    if claim is None:
        return None
    if not claim.preflighted:
        raise RealtimeVoiceIngressError("realtime event bypassed canonical preflight")
    if claim.binding.routing_key != routing_key:
        raise RealtimeVoiceIngressError(
            "realtime route changed before canonical slot claim"
        )
    if claim.slot_claimed:
        raise RealtimeVoiceIngressError("canonical realtime claim was already used")
    is_running = getattr(runner, "_is_session_running", None)
    if callable(is_running) and is_running(routing_key):
        raise RealtimeVoiceIngressError("canonical realtime route is busy")
    return claim


def _commit_realtime_voice_slot_claim(
    runner: object,
    event: object,
    routing_key: str,
    claim: _CanonicalClaim | None,
) -> None:
    """Commit the exact prepared claim without yielding or host callbacks."""

    if claim is None:
        return
    if (
        type(claim) is not _CanonicalClaim
        or getattr(event, _CLAIM_ATTR, None) is not claim
        or claim.host._runner_ref() is not runner
        or claim.binding.routing_key != routing_key
        or not claim.preflighted
        or claim.slot_claimed
    ):
        raise RealtimeVoiceIngressError(
            "canonical realtime slot claim changed before commit"
        )
    claim.slot_claimed = True


def _validate_realtime_voice_event_after_resolution(
    runner: object, event: object, session_entry: object
) -> bool:
    claim = _claim_for(runner, event)
    if claim is None:
        return False
    if not claim.preflighted or not claim.slot_claimed:
        raise RealtimeVoiceIngressError(
            "realtime event bypassed canonical routing-slot claim"
        )
    if session_entry is not claim.captured_entry:
        raise RealtimeVoiceIngressError(
            "resolved session is not the exact captured session entry"
        )
    if (
        session_entry.session_key != claim.binding.routing_key
        or session_entry.session_id != claim.binding.durable_session_id
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
    terminal_assistants = [
        row
        for row in rows
        if row["id"] > user["id"] and _is_terminal_assistant_row(row)
    ]
    if len(terminal_assistants) != 1:
        raise RealtimeVoiceIngressError(
            "exact terminal canonical assistant row was not durably persisted"
        )
    assistant = terminal_assistants[0]
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
    "NativeRealtimeOutputReservation",
    "RealtimeVoiceFinalizationReceipt",
    "RealtimeVoiceIngressError",
]
