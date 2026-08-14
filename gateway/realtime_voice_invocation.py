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
    adapter_ref: weakref.ReferenceType[object]
    host_state: "_GatewayHostState"
    entry_ref: weakref.ReferenceType[object]
    routing_generation: int
    binding: _RealtimeVoiceAttachmentBinding


@dataclass(frozen=True, slots=True)
class _ExecutionAttachmentRecord:
    runner_ref: weakref.ReferenceType[object]
    adapter_ref: weakref.ReferenceType[object]
    host_state: "_GatewayHostState"
    entry_ref: weakref.ReferenceType[object]
    routing_generation: int
    binding: _RealtimeVoiceAttachmentBinding
    route_pin: object
    invocation_identity: object


@dataclass(slots=True)
class _InvocationState:
    runner: object
    adapter: object
    host_state: "_GatewayHostState"
    entry: object
    routing_generation: int
    binding: _RealtimeVoiceAttachmentBinding
    execution_route_pin: object | None
    owner_task: asyncio.Task[Any] | None
    owner_thread_id: int
    provisional: tuple["RealtimeVoiceAttachmentFactory", _FactoryRecord] | None
    execution_provisional: tuple[object, _ExecutionAttachmentRecord] | None
    execution_minted: bool
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
_execution_attachment_records: weakref.WeakKeyDictionary[
    object, _ExecutionAttachmentRecord
]
_execution_provisional_states: weakref.WeakKeyDictionary[object, _InvocationState]
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
                adapter_ref=weakref.ref(state.adapter),
                host_state=state.host_state,
                entry_ref=weakref.ref(state.entry),
                routing_generation=state.routing_generation,
                binding=state.binding,
            )
            state.provisional = (factory, record)
            return factory

    def capture_realtime_execution_attachment(self) -> object:
        """Mint this invocation's one opaque canonical execution attachment."""

        with _state_lock:
            state = _invocation_states.get(self)
            if state is None or not state.active:
                raise RealtimeVoiceInvocationError(
                    "realtime execution capture requires an active opted-in plugin command"
                )
            if threading.get_ident() != state.owner_thread_id:
                raise RealtimeVoiceInvocationError(
                    "realtime execution capture is limited to the gateway dispatch thread"
                )
            try:
                current_task = asyncio.current_task()
            except RuntimeError:
                current_task = None
            if current_task is not state.owner_task:
                raise RealtimeVoiceInvocationError(
                    "realtime execution capture is limited to the exact gateway dispatch task"
                )
            if state.execution_minted:
                raise RealtimeVoiceInvocationError(
                    "exactly one realtime execution attachment may be minted per invocation"
                )

            from gateway.config import Platform
            from gateway.external_tool_batch import (
                ExternalToolBatchRouteChanged,
                revalidate_route_owned_agent,
            )
            from gateway.realtime_execution_attachment import (
                _mint_realtime_execution_attachment,
            )
            from gateway.session import SessionSource

            resolve_adapter = getattr(state.runner, "_adapter_for_source", None)
            source = SessionSource(
                platform=Platform.DISCORD,
                chat_id=state.binding.chat_id,
                chat_type=state.binding.chat_type,
                user_id=state.binding.principal_id,
                thread_id=state.binding.thread_id,
                scope_id=state.binding.scope_id,
                profile=state.binding.profile,
                is_bot=False,
            )
            if (
                not callable(resolve_adapter)
                or resolve_adapter(source) is not state.adapter
                or getattr(state.adapter, "gateway_runner", None) is not state.runner
            ):
                raise RealtimeVoiceInvocationError(
                    "realtime execution route adapter changed before capture"
                )
            route_pin = state.execution_route_pin
            if route_pin is None:
                raise RealtimeVoiceInvocationError(
                    "realtime execution route authority changed before capture"
                )
            try:
                revalidate_route_owned_agent(state.runner, route_pin)
            except ExternalToolBatchRouteChanged as exc:
                raise RealtimeVoiceInvocationError(
                    "realtime execution route authority changed before capture"
                ) from exc
            snapshot = state.runner.session_store.get_exact_session_entry_snapshot(
                state.binding.routing_key
            )
            if (
                snapshot[0] is not state.entry
                or snapshot[1] != state.routing_generation
                or route_pin.session_entry is not state.entry
                or route_pin.session_id != state.binding.durable_session_id
            ):
                raise RealtimeVoiceInvocationError(
                    "realtime execution session authority changed before capture"
                )
            attachment = _mint_realtime_execution_attachment()
            record = _ExecutionAttachmentRecord(
                runner_ref=weakref.ref(state.runner),
                adapter_ref=weakref.ref(state.adapter),
                host_state=state.host_state,
                entry_ref=weakref.ref(state.entry),
                routing_generation=state.routing_generation,
                binding=state.binding,
                route_pin=route_pin,
                invocation_identity=self,
            )
            state.execution_provisional = (attachment, record)
            state.execution_minted = True
            _execution_provisional_states[attachment] = state
            return attachment


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

    async def open(
        self,
        provider_name: str,
        setup: object,
        *,
        provider_session_id: str,
        required_capabilities: object = frozenset(),
    ) -> object:
        """Open one gateway-owned provider/controller attachment."""

        with _state_lock:
            record = _factory_records.get(self)
            if record is not None:
                if self in _consumed_factories:
                    raise RealtimeVoiceInvocationError(
                        "realtime attachment factory was already consumed"
                    )
                # Reserve before any validation or provider await.  An open
                # attempt is the factory's single use even when setup fails.
                _consumed_factories.add(self)
        runner = record.runner_ref() if record is not None else None
        if runner is None:
            raise RealtimeVoiceInvocationError("factory is stale or was not committed")
        from gateway.realtime_voice_messaging_host import _open_attachment

        return await _open_attachment(
            self,
            runner,
            provider_name,
            setup,
            provider_session_id=provider_session_id,
            required_capabilities=required_capabilities,
        )


_invocation_states = weakref.WeakKeyDictionary()
_factory_records = weakref.WeakKeyDictionary()
_execution_attachment_records = weakref.WeakKeyDictionary()
_execution_provisional_states = weakref.WeakKeyDictionary()
_consumed_factories = weakref.WeakSet()
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

    resolve_adapter = getattr(runner, "_adapter_for_source", None)
    adapter = resolve_adapter(source) if callable(resolve_adapter) else None
    registered_adapters = getattr(runner, "adapters", None) or {}
    profile_adapters = getattr(runner, "_profile_adapters", None) or {}
    adapter_is_registered = adapter is registered_adapters.get(Platform.DISCORD) or any(
        adapter is adapters.get(Platform.DISCORD)
        for adapters in profile_adapters.values()
    )
    if (
        adapter is None
        or not adapter_is_registered
        or getattr(adapter, "gateway_runner", None) is not runner
    ):
        raise RealtimeVoiceInvocationError(
            "realtime attachment requires the exact live Discord adapter"
        )

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
    from gateway.external_tool_batch import (
        ExternalToolBatchRouteChanged,
        pin_route_owned_agent,
    )

    try:
        execution_route_pin = pin_route_owned_agent(runner, route)
    except ExternalToolBatchRouteChanged:
        # Contextual commands and the pre-existing voice attachment remain
        # available without an active canonical turn.  Only execution capture
        # requires this exact invocation-time route authority.
        execution_route_pin = None
    try:
        owner_task = asyncio.current_task()
    except RuntimeError:
        owner_task = None
    invocation = PluginCommandInvocation(_MINT)
    state = _InvocationState(
        runner=runner,
        adapter=adapter,
        host_state=host_state,
        entry=entry,
        routing_generation=generation,
        binding=binding,
        execution_route_pin=execution_route_pin,
        owner_task=owner_task,
        owner_thread_id=threading.get_ident(),
        provisional=None,
        execution_provisional=None,
        execution_minted=False,
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
            if succeeded and state.execution_provisional is not None:
                attachment, record = state.execution_provisional
                _execution_attachment_records[attachment] = record
            if state.execution_provisional is not None:
                _execution_provisional_states.pop(
                    state.execution_provisional[0], None
                )
            state.provisional = None
            state.execution_provisional = None


def _is_host_realtime_execution_attachment(value: object) -> bool:
    from gateway.realtime_execution_attachment import RealtimeExecutionAttachment

    if type(value) is not RealtimeExecutionAttachment:
        return False
    with _state_lock:
        return value in _execution_attachment_records


def _record_for_realtime_execution_attachment(
    value: object, runner: object
) -> _ExecutionAttachmentRecord:
    """Return the private record only while every captured authority is exact."""

    from gateway.config import Platform
    from gateway.external_tool_batch import (
        ExternalToolBatchRouteChanged,
        revalidate_route_owned_agent,
    )
    from gateway.realtime_execution_attachment import RealtimeExecutionAttachment
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    if type(value) is not RealtimeExecutionAttachment or type(runner) is not GatewayRunner:
        raise RealtimeVoiceInvocationError(
            "realtime execution attachment was not issued by this host"
        )
    with _state_lock:
        record = _execution_attachment_records.get(value)
        current_host_state = _gateway_hosts.get(runner)
    if (
        record is None
        or record.runner_ref() is not runner
        or current_host_state is not record.host_state
    ):
        raise RealtimeVoiceInvocationError(
            "realtime execution attachment authority changed"
        )

    adapter = record.adapter_ref()
    resolve_adapter = getattr(runner, "_adapter_for_source", None)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id=record.binding.chat_id,
        chat_type=record.binding.chat_type,
        user_id=record.binding.principal_id,
        thread_id=record.binding.thread_id,
        scope_id=record.binding.scope_id,
        profile=record.binding.profile,
        is_bot=False,
    )
    snapshot = runner.session_store.get_exact_session_entry_snapshot(
        record.binding.routing_key
    )
    try:
        revalidate_route_owned_agent(runner, record.route_pin)
    except ExternalToolBatchRouteChanged as exc:
        raise RealtimeVoiceInvocationError(
            "realtime execution attachment authority changed"
        ) from exc
    if (
        adapter is None
        or not callable(resolve_adapter)
        or resolve_adapter(source) is not adapter
        or getattr(adapter, "gateway_runner", None) is not runner
        or snapshot[0] is not record.entry_ref()
        or snapshot[1] != record.routing_generation
        or snapshot[0] is not record.route_pin.session_entry
        or snapshot[0].session_id != record.binding.durable_session_id
    ):
        raise RealtimeVoiceInvocationError(
            "realtime execution attachment authority changed"
        )
    return record


def _is_realtime_execution_attachment_closed(value: object) -> bool:
    with _state_lock:
        return (
            value not in _execution_attachment_records
            and value not in _execution_provisional_states
        )


def _close_realtime_execution_attachment(value: object) -> None:
    from gateway.realtime_execution_attachment import RealtimeExecutionAttachment

    if type(value) is not RealtimeExecutionAttachment:
        raise RealtimeVoiceInvocationError(
            "realtime execution attachment was not issued by this host"
        )
    with _state_lock:
        _execution_attachment_records.pop(value, None)
        state = _execution_provisional_states.pop(value, None)
        if (
            state is not None
            and state.execution_provisional is not None
            and state.execution_provisional[0] is value
        ):
            state.execution_provisional = None


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

    from gateway.config import Platform
    from gateway.session import SessionSource

    adapter = record.adapter_ref()
    resolve_adapter = getattr(runner, "_adapter_for_source", None)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id=record.binding.chat_id,
        chat_type=record.binding.chat_type,
        user_id=record.binding.principal_id,
        thread_id=record.binding.thread_id,
        scope_id=record.binding.scope_id,
        profile=record.binding.profile,
        is_bot=False,
    )
    if (
        adapter is None
        or getattr(adapter, "gateway_runner", None) is not runner
        or not callable(resolve_adapter)
        or resolve_adapter(source) is not adapter
    ):
        raise RealtimeVoiceInvocationError(
            "factory Discord adapter is no longer the exact live route adapter"
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


def _record_for_realtime_voice_attachment_factory(
    value: object, runner: object
) -> _FactoryRecord:
    """Return the exact live private record after full host validation."""

    _validate_realtime_voice_attachment_factory(value, runner)
    with _state_lock:
        record = _factory_records.get(value)
    if record is None:
        raise RealtimeVoiceInvocationError("factory was not issued by this host")
    return record


def _binding_for_realtime_voice_attachment_factory(
    value: object, runner: object
) -> _RealtimeVoiceAttachmentBinding:
    """Backward-compatible private alias for the exact host validation seam."""

    return _validate_realtime_voice_attachment_factory(value, runner)
