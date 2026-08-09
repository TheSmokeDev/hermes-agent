"""Invocation-scoped authority for gateway realtime voice attachment.

The gateway plugin context is process-global.  This module deliberately keeps
current-message authority in a :class:`ContextVar` instead, and additionally
binds each invocation to the exact dispatch task so a task spawned by a plugin
cannot inherit usable authority after the command handler returns.

This is only the capture seam.  Constructing a realtime controller and
submitting transcripts remain responsibilities of later host-owned code.
"""

from __future__ import annotations

import asyncio
import threading
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Iterator


class RealtimeVoiceInvocationError(PermissionError):
    """Raised when realtime attachment authority is unavailable or invalid."""


@dataclass(frozen=True, slots=True)
class _RealtimeVoiceAttachmentBinding:
    """Immutable host projection captured without retaining gateway objects."""

    platform: str
    chat_id: str
    chat_type: str
    principal_id: str
    thread_id: str | None
    scope_id: str | None
    profile: str | None
    routing_key: str
    durable_session_id: str


class RealtimeVoiceAttachmentFactory:
    """Opaque, host-issued capability captured by a gateway plugin command.

    Plugins may retain this exact object for the later attachment operation.
    It intentionally exposes no source, runner, routing, or principal fields,
    cannot be serialized, and cannot be reconstructed from its repr.
    """

    __slots__ = ("__binding", "__weakref__")

    def __init__(self, binding: _RealtimeVoiceAttachmentBinding) -> None:
        self.__binding = binding

    def __repr__(self) -> str:
        return "<host realtime voice attachment factory>"

    def __reduce__(self):
        raise TypeError("realtime attachment factories cannot be serialized")

_issued_factories: weakref.WeakSet[RealtimeVoiceAttachmentFactory] = weakref.WeakSet()
_issued_factories_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class _RealtimeVoicePluginInvocation:
    platform: str
    chat_id: str
    chat_type: str
    principal_id: str | None
    thread_id: str | None
    scope_id: str | None
    profile: str | None
    routing_key: str
    durable_session_id: Callable[[], str | None]
    owner_task: asyncio.Task[object] | None
    owner_thread_id: int

    def capture(self) -> RealtimeVoiceAttachmentFactory:
        if threading.get_ident() != self.owner_thread_id:
            raise RealtimeVoiceInvocationError(
                "realtime attachment capture is limited to the gateway dispatch thread"
            )
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        if current_task is not self.owner_task:
            raise RealtimeVoiceInvocationError(
                "realtime attachment capture is limited to the exact gateway dispatch task"
            )
        if not self.principal_id:
            raise RealtimeVoiceInvocationError(
                "realtime attachment requires an authenticated invocation principal"
            )
        try:
            durable_session_id = self.durable_session_id()
        except Exception as exc:
            raise RealtimeVoiceInvocationError(
                "could not resolve the existing durable session mapping"
            ) from exc
        if not isinstance(durable_session_id, str) or not durable_session_id:
            raise RealtimeVoiceInvocationError(
                "realtime attachment requires an existing durable session mapping"
            )

        binding = _RealtimeVoiceAttachmentBinding(
            platform=self.platform,
            chat_id=self.chat_id,
            chat_type=self.chat_type,
            principal_id=self.principal_id,
            thread_id=self.thread_id,
            scope_id=self.scope_id,
            profile=self.profile,
            routing_key=self.routing_key,
            durable_session_id=durable_session_id,
        )
        factory = RealtimeVoiceAttachmentFactory(binding)
        with _issued_factories_lock:
            _issued_factories.add(factory)
        return factory


_current_invocation: ContextVar[_RealtimeVoicePluginInvocation | None] = ContextVar(
    "gateway_realtime_voice_plugin_invocation", default=None
)


def capture_realtime_voice_attachment_factory() -> RealtimeVoiceAttachmentFactory:
    """Capture the exact host capability for the active plugin command.

    This fails closed outside an authenticated gateway plugin-command dispatch,
    including calls inherited by child/background tasks.
    """

    invocation = _current_invocation.get()
    if invocation is None:
        raise RealtimeVoiceInvocationError(
            "realtime attachment capture requires an active gateway plugin command"
        )
    return invocation.capture()


@contextmanager
def realtime_voice_plugin_invocation(
    *,
    source: object,
    routing_key: str,
    durable_session_id: Callable[[], str | None],
) -> Iterator[None]:
    """Bind one authenticated gateway event around its plugin handler call."""

    try:
        owner_task = asyncio.current_task()
    except RuntimeError:
        owner_task = None
    invocation = _RealtimeVoicePluginInvocation(
        platform=str(getattr(getattr(source, "platform", None), "value", "")),
        chat_id=str(getattr(source, "chat_id", "")),
        chat_type=str(getattr(source, "chat_type", "")),
        principal_id=(
            str(principal) if (principal := getattr(source, "user_id", None)) else None
        ),
        thread_id=(
            str(thread_id)
            if (thread_id := getattr(source, "thread_id", None)) is not None
            else None
        ),
        scope_id=(
            str(scope_id)
            if (scope_id := getattr(source, "scope_id", None)) is not None
            else None
        ),
        profile=(
            str(profile)
            if (profile := getattr(source, "profile", None)) is not None
            else None
        ),
        routing_key=str(routing_key),
        durable_session_id=durable_session_id,
        owner_task=owner_task,
        owner_thread_id=threading.get_ident(),
    )
    token = _current_invocation.set(invocation)
    try:
        yield
    finally:
        _current_invocation.reset(token)


def _is_host_realtime_voice_attachment_factory(value: object) -> bool:
    """Return whether *value* is the exact live object minted by this host."""

    if type(value) is not RealtimeVoiceAttachmentFactory:
        return False
    with _issued_factories_lock:
        return value in _issued_factories


def _binding_for_realtime_voice_attachment_factory(
    value: object,
) -> _RealtimeVoiceAttachmentBinding:
    """Resolve a factory for host code, rejecting copies and lookalikes."""

    if not _is_host_realtime_voice_attachment_factory(value):
        raise RealtimeVoiceInvocationError(
            "realtime attachment factory was not issued by this host"
        )
    return value._RealtimeVoiceAttachmentFactory__binding
