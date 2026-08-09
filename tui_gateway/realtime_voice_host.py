"""Concrete same-session turn host for TUI realtime voice attachments."""

from __future__ import annotations

import asyncio
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from agent.realtime_voice_admission import RealtimeSessionBinding, RealtimeUtterance
from gateway.realtime_voice_controller import HostProjection, HostProjectionStatus

_REQUEST_ID = "realtime-voice"
_ATTACHMENT_KEY = "_realtime_voice_attachment"


class RealtimeTurnHostError(RuntimeError):
    """The host could not prove or complete a same-session operation."""


@dataclass(frozen=True, slots=True)
class TuiRealtimeTurnReceipt:
    """Immutable positive claim returned by the canonical prompt handler."""

    status: str
    binding: RealtimeSessionBinding
    provider_turn_id: str
    item_id: str


class _Permit:
    __slots__ = ()


class TuiRealtimeTurnHost:
    """Bind realtime transcript admission to one exact live TUI attachment."""

    def __init__(
        self,
        server: Any,
        binding: RealtimeSessionBinding,
        *,
        session: dict[str, Any],
        transport: object,
        principal: object,
        validate_principal: Callable[[dict[str, Any], object, object], bool],
        projection_sink: Callable[[HostProjection], Awaitable[None]],
    ) -> None:
        self._server = server
        self._binding = binding
        self._session = session
        self._transport = transport
        self._principal = principal
        if not callable(validate_principal):
            raise TypeError("validate_principal must be an independent capability callback")
        self._validate_principal = validate_principal
        if not callable(projection_sink):
            raise TypeError("projection_sink must be an async host projection sink")
        self._projection_sink = projection_sink
        try:
            self._projection_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._projection_loop = asyncio.get_event_loop_policy().get_event_loop()
        self._attachment_capability = object()
        self._projection_lock = threading.Lock()
        self._projection_rank = -1
        self._terminal_projection = False
        self._finalization_receipts: set[object] = set()
        self._canonical_work_pending = False
        self._active_turn_id: str | None = None
        try:
            self._prompt_submit = server._methods["prompt.submit"]
            self._session_interrupt = server._methods["session.interrupt"]
        except (AttributeError, KeyError) as exc:
            raise ValueError("server must have installed realtime host handlers") from exc
        self._lock = asyncio.Lock()
        self._permits: dict[_Permit, tuple[RealtimeSessionBinding, RealtimeUtterance]] = {}
        history_lock = session.get("history_lock")
        if history_lock is None:
            raise ValueError("session must expose its canonical history lock")
        attachment = {
            "binding": binding,
            "captured_binding": binding,
            "captured_session": session,
            "capability": self._attachment_capability,
            "host": self,
            "loop": self._projection_loop,
            "principal": principal,
            "captured_principal": principal,
            "provider_session_id": binding.provider_session_id,
            "routing_key": binding.routing_key,
            "selection_generation": binding.selection_generation,
            "transport": transport,
            "captured_transport": transport,
            "validate_principal": validate_principal,
            "captured_validate_principal": validate_principal,
        }
        self._attachment_record = attachment
        with history_lock:
            if not self._base_session_is_exact():
                raise ValueError("realtime attachment does not match the live TUI session")
            if _ATTACHMENT_KEY in session:
                raise ValueError("TUI session is already attached to realtime voice")
            session[_ATTACHMENT_KEY] = attachment

    @staticmethod
    def _session_profile_id(session: dict[str, Any]) -> str:
        explicit = session.get("profile_name") or session.get("profile_id")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        profile_home = session.get("profile_home")
        if isinstance(profile_home, (str, Path)) and str(profile_home).strip():
            return Path(profile_home).name
        return "default"

    def _principal_is_live(self) -> bool:
        try:
            return bool(self._validate_principal(
                self._session, self._transport, self._principal
            ))
        except BaseException:
            return False

    def _uses_compute_host(self) -> bool:
        checker = getattr(self._server, "_session_uses_compute_host", None)
        if checker is None:
            return False
        try:
            return bool(checker(self._session))
        except BaseException:
            return True

    def _base_session_is_exact(self) -> bool:
        return bool(
            self._server._sessions.get(self._binding.runtime_session_id) is self._session
            and not self._uses_compute_host()
            and self._session.get("session_key") == self._binding.durable_session_id
            and self._session_profile_id(self._session) == self._binding.profile_id
            and self._session.get("transport") is self._transport
            and self._principal_is_live()
        )

    def _attachment_running_if_exact(self) -> bool | None:
        if self._uses_compute_host():
            return None
        current = self._server._sessions.get(self._binding.runtime_session_id)
        if current is not self._session:
            return None
        history_lock = self._session.get("history_lock")
        if history_lock is None:
            return None
        with history_lock:
            attachment = self._session.get(_ATTACHMENT_KEY)
            if not (
                self._base_session_is_exact()
                and isinstance(attachment, dict)
                and attachment is self._attachment_record
                and attachment.get("binding") is self._binding
                and attachment.get("captured_binding") is self._binding
                and attachment.get("captured_session") is self._session
                and attachment.get("capability") is self._attachment_capability
                and attachment.get("host") is self
                and attachment.get("loop") is self._projection_loop
                and attachment.get("transport") is self._transport
                and attachment.get("captured_transport") is self._transport
                and attachment.get("principal") is self._principal
                and attachment.get("captured_principal") is self._principal
                and attachment.get("validate_principal") is self._validate_principal
                and attachment.get("captured_validate_principal")
                is self._validate_principal
                and attachment.get("provider_session_id")
                == self._binding.provider_session_id
                and attachment.get("routing_key") == self._binding.routing_key
                and attachment.get("selection_generation")
                == self._binding.selection_generation
            ):
                return None
            return self._session.get("running") is True

    def _attachment_is_exact(self) -> bool:
        return self._attachment_running_if_exact() is not None

    async def authorize(
        self,
        binding: RealtimeSessionBinding,
        utterance: RealtimeUtterance,
    ) -> object | None:
        async with self._lock:
            if (
                binding != self._binding
                or utterance.provider_session_id != binding.provider_session_id
                or not self._attachment_is_exact()
                or self._canonical_work_pending
            ):
                return None
            permit = _Permit()
            self._permits[permit] = (binding, utterance)
            return permit

    async def revoke(self, permit: object) -> None:
        async with self._lock:
            self._attachment_is_exact()
            self._permits.pop(permit, None)  # type: ignore[arg-type]

    async def submit(
        self,
        binding: RealtimeSessionBinding,
        utterance: RealtimeUtterance,
        permit: object,
    ) -> TuiRealtimeTurnReceipt:
        async with self._lock:
            owned = self._permits.pop(permit, None)  # type: ignore[arg-type]
            if (
                owned is None
                or owned[0] != binding
                or owned[1] != utterance
                or binding != self._binding
                or not self._attachment_is_exact()
            ):
                raise RealtimeTurnHostError("invalid, foreign, or consumed permit")
            self._canonical_work_pending = True
            self._active_turn_id = secrets.token_urlsafe(32)
            with self._projection_lock:
                self._projection_rank = -1
                self._terminal_projection = False
            try:
                response = self._prompt_submit(
                    _REQUEST_ID,
                    {
                        "session_id": self._binding.runtime_session_id,
                        "text": utterance.text,
                        "queued": True,
                        "_trusted_realtime_attachment": self._attachment_capability,
                        "_trusted_realtime_turn_id": self._active_turn_id,
                    },
                )
            except BaseException as exc:
                self._canonical_work_pending = False
                self._active_turn_id = None
                raise RealtimeTurnHostError("canonical prompt enqueue failed") from exc
            status = self._positive_status(response, {"streaming", "queued"})
            if status is None:
                self._canonical_work_pending = False
                self._active_turn_id = None
                raise RealtimeTurnHostError("canonical prompt enqueue was not accepted")
            return TuiRealtimeTurnReceipt(
                status=status,
                binding=self._binding,
                provider_turn_id=utterance.provider_turn_id,
                item_id=utterance.item_id,
            )

    async def interrupt_and_wait(
        self,
        binding: RealtimeSessionBinding,
        timeout: float,
    ) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout must be a positive finite number")
        if timeout <= 0 or timeout == float("inf") or timeout != timeout:
            raise ValueError("timeout must be a positive finite number")
        async with self._lock:
            if binding != self._binding or not self._attachment_is_exact():
                raise RealtimeTurnHostError("realtime attachment changed")
            try:
                response = self._session_interrupt(
                    _REQUEST_ID,
                    {"session_id": self._binding.runtime_session_id},
                )
            except BaseException as exc:
                raise RealtimeTurnHostError("canonical interrupt failed") from exc
            if self._positive_status(response, {"interrupted"}) is None:
                raise RealtimeTurnHostError("canonical interrupt was not accepted")

            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                running = self._attachment_running_if_exact()
                if running is None:
                    raise RealtimeTurnHostError("realtime attachment changed")
                if not running:
                    return
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise RealtimeTurnHostError(
                        "canonical interrupt completion timed out"
                    )
                await asyncio.sleep(min(0.01, remaining))

    async def close_attachment(
        self, binding: RealtimeSessionBinding | None
    ) -> None:
        """Revoke this host-owned attachment capability without touching accepted work."""
        async with self._lock:
            self._permits.clear()
            if binding != self._binding:
                return
            history_lock = self._session.get("history_lock")
            if history_lock is None:
                return
            with history_lock:
                attachment = self._session.get(_ATTACHMENT_KEY)
                if attachment is self._attachment_record:
                    self._session.pop(_ATTACHMENT_KEY, None)

    def _notify_from_production(
        self,
        capability: object,
        status: HostProjectionStatus,
        *,
        detail: str = "",
        agent: object | None = None,
        expected_user: object | None = None,
        expected_assistant: object | None = None,
        allow_host_drift: bool = False,
    ) -> bool:
        """Accept one canonical server hook and schedule the private async sink."""
        if capability is not self._attachment_capability:
            return False
        if not self._attachment_is_exact() and not (
            allow_host_drift
            and status is HostProjectionStatus.FAILED
            and self._session.get(_ATTACHMENT_KEY) is self._attachment_record
        ):
            return False
        if not self._canonical_work_pending:
            return False

        finalization: object | None = None
        if status is HostProjectionStatus.COMPLETED and not self._durable_tail_is_exact(
            agent, expected_user, expected_assistant
        ):
            status = HostProjectionStatus.FAILED
            detail = "canonical turn was not durably persisted"

        rank = {
            HostProjectionStatus.THINKING: 0,
            HostProjectionStatus.ACTING: 1,
            HostProjectionStatus.SPEAKING: 2,
            HostProjectionStatus.COMPLETED: 3,
            HostProjectionStatus.FAILED: 3,
        }[status]
        with self._projection_lock:
            if self._terminal_projection or rank < self._projection_rank:
                return False
            if rank == self._projection_rank and rank < 3:
                return False
            if rank == 3:
                self._terminal_projection = True
                self._canonical_work_pending = False
                if status is HostProjectionStatus.COMPLETED:
                    finalization = object()
                    self._finalization_receipts.add(finalization)
            self._projection_rank = rank
            projection = HostProjection(self._binding, status, detail, finalization)
            if rank == 3:
                self._active_turn_id = None
            self._projection_loop.call_soon_threadsafe(
                self._schedule_projection, projection
            )
        return True

    def _schedule_projection(self, projection: HostProjection) -> None:
        task = self._projection_loop.create_task(self._projection_sink(projection))

        def consume_result(done: asyncio.Task[None]) -> None:
            try:
                done.result()
            except BaseException:
                pass

        task.add_done_callback(consume_result)

    def _durable_tail_is_exact(
        self,
        agent: object | None,
        expected_user: object | None,
        expected_assistant: object | None,
    ) -> bool:
        if (
            agent is None
            or expected_user is None
            or expected_assistant is None
            or self._active_turn_id is None
        ):
            return False
        db = getattr(agent, "_session_db", None)
        durable_id = getattr(agent, "session_id", None) or self._session.get("session_key")
        if db is None or durable_id != self._session.get("session_key"):
            return False
        try:
            messages = db.get_messages_as_conversation(durable_id)
        except BaseException:
            return False
        assistant_index = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if messages[index].get("role") == "assistant"
                and messages[index].get("content") == expected_assistant
            ),
            None,
        )
        if assistant_index is None:
            return False
        return any(
            message.get("role") == "user"
            and message.get("content") == expected_user
            and message.get("display_kind") == "realtime_voice"
            and (message.get("display_metadata") or {}).get("turn_id")
            == self._active_turn_id
            for message in messages[:assistant_index]
        )

    def validate_finalization(self, receipt: object) -> bool:
        """Consume once a receipt minted privately after durable read-back."""
        with self._projection_lock:
            if receipt not in self._finalization_receipts:
                return False
            self._finalization_receipts.remove(receipt)
            return True


    @staticmethod
    def _positive_status(response: object, accepted: set[str]) -> str | None:
        if not isinstance(response, dict) or "error" in response:
            return None
        result = response.get("result")
        if not isinstance(result, dict):
            return None
        status = result.get("status")
        return status if isinstance(status, str) and status in accepted else None


def validate_realtime_prompt_attachment(
    session: dict[str, Any], proof: object, turn_id: object
) -> bool:
    """Validate an in-process attachment proof while prompt.submit owns history_lock."""
    attachment = session.get(_ATTACHMENT_KEY)
    if not isinstance(attachment, dict) or attachment.get("capability") is not proof:
        return False
    binding = attachment.get("binding")
    captured_binding = attachment.get("captured_binding")
    captured_session = attachment.get("captured_session")
    transport = attachment.get("transport")
    captured_transport = attachment.get("captured_transport")
    principal = attachment.get("principal")
    captured_principal = attachment.get("captured_principal")
    principal_validator = attachment.get("validate_principal")
    captured_principal_validator = attachment.get("captured_validate_principal")
    host = attachment.get("host")
    loop = attachment.get("loop")
    if (
        not isinstance(binding, RealtimeSessionBinding)
        or binding is not captured_binding
        or session is not captured_session
        or transport is not captured_transport
        or principal is not captured_principal
        or principal_validator is not captured_principal_validator
        or not callable(principal_validator)
        or not isinstance(host, TuiRealtimeTurnHost)
        or host._session is not session
        or attachment is not host._attachment_record
        or host._binding is not binding
        or host._attachment_capability is not proof
        or host._uses_compute_host()
        or not isinstance(turn_id, str)
        or turn_id != host._active_turn_id
        or loop is not host._projection_loop
    ):
        return False
    try:
        principal_is_live = principal_validator(session, transport, principal)
    except BaseException:
        return False
    return bool(
        principal_is_live
        and session.get("session_key") == binding.durable_session_id
        and TuiRealtimeTurnHost._session_profile_id(session) == binding.profile_id
        and session.get("transport") is transport
        and attachment.get("provider_session_id") == binding.provider_session_id
        and attachment.get("routing_key") == binding.routing_key
        and attachment.get("selection_generation") == binding.selection_generation
    )


def notify_realtime_turn(
    session: dict[str, Any],
    status: HostProjectionStatus,
    *,
    detail: str = "",
    agent: object | None = None,
    expected_user: object | None = None,
    expected_assistant: object | None = None,
    allow_host_drift: bool = False,
) -> bool:
    """Bridge synchronous canonical TUI hooks to the exact attached host."""
    attachment = session.get(_ATTACHMENT_KEY)
    if not isinstance(attachment, dict):
        return False
    host = attachment.get("host")
    capability = attachment.get("capability")
    if (
        not isinstance(host, TuiRealtimeTurnHost)
        or host._session is not session
        or attachment is not host._attachment_record
        or attachment.get("loop") is not host._projection_loop
        or capability is not host._attachment_capability
    ):
        return False
    return host._notify_from_production(
        capability,
        status,
        detail=detail,
        agent=agent,
        expected_user=expected_user,
        expected_assistant=expected_assistant,
        allow_host_drift=allow_host_drift,
    )


__all__ = [
    "RealtimeTurnHostError",
    "TuiRealtimeTurnHost",
    "TuiRealtimeTurnReceipt",
    "notify_realtime_turn",
    "validate_realtime_prompt_attachment",
]
