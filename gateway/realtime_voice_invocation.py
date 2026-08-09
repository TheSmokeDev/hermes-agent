"""Host-owned invocation capability for Discord realtime attachment capture.

Only an explicitly opted-in plugin command receives a ``PluginCommandInvocation``.
The object can capture an opaque factory while that exact authenticated gateway
handler is running. Factories are provisional until the handler returns
successfully and remain bound to the exact runner and live routing entry.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import weakref
from dataclasses import dataclass
from typing import Any, Callable


class RealtimeVoiceInvocationError(PermissionError):
    """Raised when realtime attachment authority is unavailable or invalid."""


@dataclass(frozen=True, slots=True)
class _RealtimeVoiceAttachmentBinding:
    platform: str
    chat_id: str
    chat_type: str
    principal_id: str
    thread_id: str | None
    scope_id: str
    profile: str | None
    routing_key: str
    durable_session_id: str


@dataclass(frozen=True, slots=True)
class _FactoryRecord:
    runner_ref: weakref.ReferenceType[object]
    host_state: "_GatewayHostState"
    entry_ref: weakref.ReferenceType[object]
    routing_generation: int
    binding: _RealtimeVoiceAttachmentBinding


@dataclass(slots=True)
class _InvocationState:
    runner: object
    host_state: "_GatewayHostState"
    entry: object
    routing_generation: int
    binding: _RealtimeVoiceAttachmentBinding
    owner_task: asyncio.Task[Any] | None
    owner_thread_id: int
    provisional: tuple["RealtimeVoiceAttachmentFactory", _FactoryRecord] | None
    active: bool = True


@dataclass(frozen=True, slots=True)
class _GatewayHostState:
    runner_ref: weakref.ReferenceType[object]


_MINT = object()
_state_lock = threading.RLock()
_invocation_states: weakref.WeakKeyDictionary[PluginCommandInvocation, _InvocationState]
_factory_records: weakref.WeakKeyDictionary[
    RealtimeVoiceAttachmentFactory, _FactoryRecord
]
_gateway_hosts: weakref.WeakKeyDictionary[object, _GatewayHostState]


class PluginCommandInvocation:
    """Immutable, host-minted narrow service for one opted-in command call."""

    __slots__ = ("__weakref__",)

    def __new__(cls, mint: object = None):
        if mint is not _MINT:
            raise TypeError("plugin command invocations are host-minted")
        return super().__new__(cls)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("plugin command invocations are immutable")

    def __repr__(self) -> str:
        return "<host plugin command invocation>"

    def __reduce__(self):
        raise TypeError("plugin command invocations cannot be serialized")

    def capture_realtime_voice_attachment_factory(
        self,
    ) -> "RealtimeVoiceAttachmentFactory":
        with _state_lock:
            state = _invocation_states.get(self)
            if state is None or not state.active:
                raise RealtimeVoiceInvocationError(
                    "realtime attachment capture requires an active opted-in plugin command"
                )
            if threading.get_ident() != state.owner_thread_id:
                raise RealtimeVoiceInvocationError(
                    "realtime attachment capture is limited to the gateway dispatch thread"
                )
            try:
                current_task = asyncio.current_task()
            except RuntimeError:
                current_task = None
            if current_task is not state.owner_task:
                raise RealtimeVoiceInvocationError(
                    "realtime attachment capture is limited to the exact gateway dispatch task"
                )
            if state.provisional is not None:
                return state.provisional[0]
            factory = RealtimeVoiceAttachmentFactory(_MINT)
            record = _FactoryRecord(
                runner_ref=weakref.ref(state.runner),
                host_state=state.host_state,
                entry_ref=weakref.ref(state.entry),
                routing_generation=state.routing_generation,
                binding=state.binding,
            )
            state.provisional = (factory, record)
            return factory


class RealtimeVoiceAttachmentFactory:
    """Opaque nonserializable capability retained by a successful handler."""

    __slots__ = ("__weakref__",)

    def __new__(cls, mint: object = None):
        if mint is not _MINT:
            raise TypeError("realtime attachment factories are host-minted")
        return super().__new__(cls)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("realtime attachment factories are immutable")

    def __repr__(self) -> str:
        return "<host realtime voice attachment factory>"

    def __reduce__(self):
        raise TypeError("realtime attachment factories cannot be serialized")


_invocation_states = weakref.WeakKeyDictionary()
_factory_records = weakref.WeakKeyDictionary()
_gateway_hosts = weakref.WeakKeyDictionary()


def _register_gateway_runner(runner: object) -> bool:
    """Install private mint authority during exact GatewayRunner construction."""
    from gateway.run import GatewayRunner

    if type(runner) is not GatewayRunner:
        return False
    with _state_lock:
        if runner not in _gateway_hosts:
            _gateway_hosts[runner] = _GatewayHostState(weakref.ref(runner))
    return True


def capture_realtime_voice_attachment_factory() -> RealtimeVoiceAttachmentFactory:
    """Legacy compatibility surface which never grants ambient authority."""

    raise RealtimeVoiceInvocationError(
        "realtime attachment capture requires an active gateway plugin command; "
        "an active opted-in plugin command is required"
    )


def _exact_normalized_string(value: object, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or not value or value.strip() != value:
        raise RealtimeVoiceInvocationError(
            "Discord invocation facts must be normalized strings"
        )
    return value


def _mint_invocation_state(
    *,
    runner: object,
    source: object,
    routing_key: object,
    authenticated: object,
    internal: object,
) -> tuple[PluginCommandInvocation, _InvocationState]:
    # Imports stay here to avoid making this plugin-importable module own runner setup.
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource, build_session_key

    if type(runner) is not GatewayRunner:
        raise RealtimeVoiceInvocationError(
            "invocation authority requires the exact gateway host"
        )
    with _state_lock:
        host_state = _gateway_hosts.get(runner)
    if host_state is None or host_state.runner_ref() is not runner:
        raise RealtimeVoiceInvocationError("gateway has no host-owned dispatch state")
    if authenticated is not True or internal is not False:
        raise RealtimeVoiceInvocationError(
            "invocation authority requires positive user authentication"
        )
    if type(source) is not SessionSource or source.platform is not Platform.DISCORD:
        raise RealtimeVoiceInvocationError("realtime attachment is Discord-only")

    principal_id = _exact_normalized_string(source.user_id)
    chat_id = _exact_normalized_string(source.chat_id)
    chat_type = _exact_normalized_string(source.chat_type)
    scope_id = _exact_normalized_string(source.scope_id)
    thread_id = _exact_normalized_string(source.thread_id, optional=True)
    profile = _exact_normalized_string(source.profile, optional=True)
    route = _exact_normalized_string(routing_key)
    if (
        source.is_bot is not False
        or source.guild_id != scope_id
        or chat_type not in {"group", "thread"}
        or route != build_session_key(source)
    ):
        raise RealtimeVoiceInvocationError("Discord guild routing facts are invalid")

    store = getattr(runner, "session_store", None)
    exact_snapshot = getattr(store, "get_exact_session_entry_snapshot", None)
    if not callable(exact_snapshot):
        raise RealtimeVoiceInvocationError("host has no atomic session snapshot lookup")
    entry, generation = exact_snapshot(route)
    if (
        entry is None
        or type(getattr(entry, "session_id", None)) is not str
        or not entry.session_id
    ):
        raise RealtimeVoiceInvocationError(
            "realtime attachment requires an existing durable session"
        )
    if getattr(entry, "session_key", None) != route:
        raise RealtimeVoiceInvocationError(
            "existing session routing facts do not match"
        )
    if type(generation) is not int:
        raise RealtimeVoiceInvocationError("host routing generation is unavailable")

    binding = _RealtimeVoiceAttachmentBinding(
        platform="discord",
        chat_id=chat_id,
        chat_type=chat_type,
        principal_id=principal_id,
        thread_id=thread_id,
        scope_id=scope_id,
        profile=profile,
        routing_key=route,
        durable_session_id=entry.session_id,
    )
    try:
        owner_task = asyncio.current_task()
    except RuntimeError:
        owner_task = None
    invocation = PluginCommandInvocation(_MINT)
    state = _InvocationState(
        runner=runner,
        host_state=host_state,
        entry=entry,
        routing_generation=generation,
        binding=binding,
        owner_task=owner_task,
        owner_thread_id=threading.get_ident(),
        provisional=None,
    )
    with _state_lock:
        _invocation_states[invocation] = state
    return invocation, state


async def _invoke_plugin_command_with_context(
    *,
    runner: object,
    handler: Callable[..., object],
    raw_args: str,
    source: object,
    routing_key: str,
    authenticated: bool,
    internal: bool,
) -> object:
    """Private GatewayRunner dispatch seam; commits factories only on success."""

    invocation, state = _mint_invocation_state(
        runner=runner,
        source=source,
        routing_key=routing_key,
        authenticated=authenticated,
        internal=internal,
    )
    succeeded = False
    try:
        result = handler(raw_args, invocation)
        if inspect.isawaitable(result):
            result = await result
        succeeded = True
        return result
    finally:
        with _state_lock:
            state.active = False
            _invocation_states.pop(invocation, None)
            if succeeded and state.provisional is not None:
                factory, record = state.provisional
                _factory_records[factory] = record
            state.provisional = None


def _is_host_realtime_voice_attachment_factory(value: object) -> bool:
    if type(value) is not RealtimeVoiceAttachmentFactory:
        return False
    with _state_lock:
        return value in _factory_records


def _validate_realtime_voice_attachment_factory(
    value: object, runner: object
) -> _RealtimeVoiceAttachmentBinding:
    """Validate exact runner, live entry identity, generation, and pinned facts."""

    from gateway.run import GatewayRunner

    if (
        type(value) is not RealtimeVoiceAttachmentFactory
        or type(runner) is not GatewayRunner
    ):
        raise RealtimeVoiceInvocationError("factory was not issued by this host")
    with _state_lock:
        record = _factory_records.get(value)
    with _state_lock:
        current_host_state = _gateway_hosts.get(runner)
    if (
        record is None
        or record.runner_ref() is not runner
        or current_host_state is not record.host_state
    ):
        raise RealtimeVoiceInvocationError(
            "factory belongs to a different gateway host"
        )

    store = getattr(runner, "session_store", None)
    snapshot = getattr(store, "get_exact_session_entry_snapshot", None)
    if not callable(snapshot):
        raise RealtimeVoiceInvocationError("host has no atomic session snapshot lookup")
    current, generation = snapshot(record.binding.routing_key)
    if type(generation) is not int or generation != record.routing_generation:
        raise RealtimeVoiceInvocationError("factory routing generation is stale")
    if current is not record.entry_ref():
        raise RealtimeVoiceInvocationError("factory session entry is no longer current")
    if (
        current is None
        or current.session_key != record.binding.routing_key
        or current.session_id != record.binding.durable_session_id
    ):
        raise RealtimeVoiceInvocationError("factory session identity is stale")
    return record.binding


def _binding_for_realtime_voice_attachment_factory(
    value: object, runner: object
) -> _RealtimeVoiceAttachmentBinding:
    """Backward-compatible private alias for the exact host validation seam."""

    return _validate_realtime_voice_attachment_factory(value, runner)
