"""Host-owned invocation capability for Discord realtime attachment capture.

Only an explicitly opted-in plugin command receives a ``PluginCommandInvocation``.
The object can capture an opaque factory while that exact authenticated gateway
handler is running. Factories are provisional until the handler returns
successfully and remain bound to the exact runner and live routing entry.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
import threading
import unicodedata
import weakref
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4


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
class _ToolCallPermitRecord:
    attachment_ref: weakref.ReferenceType[object]
    attachment_record: _ExecutionAttachmentRecord
    route_pin: object
    agent: object
    tool_surface: object
    valid_tool_names: object
    response_id: str
    item_id: str
    call_id: str
    batch_id: str
    tool_name: str
    arguments_bytes: bytes
    arguments_digest: bytes
    mint_generation: int
    mint_routing_generation: int
    mint_high_water: object | None
    consumed: bool = False


@dataclass(slots=True)
class _AttachmentPermitState:
    seen_identities: set[tuple[str, str, str, str]]
    permits: weakref.WeakSet[object]
    finalizer: object | None = None


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
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_REALTIME_TOOLS = 128
_MAX_TOOL_DESCRIPTION_BYTES = 16_384
_MAX_TOOL_SCHEMA_DEPTH = 16
_MAX_TOOL_SCHEMA_NODES = 4_096
_MAX_TOOL_DEFINITIONS_BYTES = 262_144
_MAX_DISCARDED_TOOL_SCHEMA_BYTES = _MAX_TOOL_DEFINITIONS_BYTES
_MAX_PROVIDER_ID_BYTES = 512
_MAX_TOOL_ARGUMENT_DEPTH = 16
_MAX_TOOL_ARGUMENT_NODES = 4_096
_MAX_TOOL_ARGUMENT_KEYS = 4_096
_MAX_TOOL_ARGUMENT_KEY_BYTES = 512
_MAX_TOOL_ARGUMENT_BYTES = 262_144
_MAX_OUTSTANDING_TOOL_CALL_PERMITS = 128
_MAX_TOOL_CALL_PERMITS_PER_BATCH = 128
_invocation_states: weakref.WeakKeyDictionary[PluginCommandInvocation, _InvocationState]
_factory_records: weakref.WeakKeyDictionary[
    RealtimeVoiceAttachmentFactory, _FactoryRecord
]
_execution_attachment_records: weakref.WeakKeyDictionary[
    object, _ExecutionAttachmentRecord
]
_execution_provisional_states: weakref.WeakKeyDictionary[object, _InvocationState]
_tool_call_permit_records: weakref.WeakKeyDictionary[object, _ToolCallPermitRecord]
_attachment_permit_states: weakref.WeakKeyDictionary[object, _AttachmentPermitState]
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
                revalidate_durable_route_owned_agent,
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
                revalidate_durable_route_owned_agent(state.runner, route_pin)
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
_tool_call_permit_records = weakref.WeakKeyDictionary()
_attachment_permit_states = weakref.WeakKeyDictionary()
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
        pin_durable_route_owned_agent,
    )

    try:
        execution_route_pin = pin_durable_route_owned_agent(
            runner, route, source=source
        )
    except (ExternalToolBatchRouteChanged, AttributeError):
        # Contextual commands and the pre-existing voice attachment remain
        # available without an active or initialized canonical execution turn.
        # Any later execution capture still fails closed on the missing pin.
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
        revalidate_durable_route_owned_agent,
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
        revalidate_durable_route_owned_agent(runner, record.route_pin)
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


def _tool_definitions_for_realtime_execution_attachment(
    value: object,
) -> list[dict[str, object]]:
    """Project the exact live agent's already-curated provider tool surface."""

    with _state_lock:
        record = _execution_attachment_records.get(value)
    runner = record.runner_ref() if record is not None else None
    if runner is None:
        raise RealtimeVoiceInvocationError(
            "realtime execution attachment authority changed"
        )
    record = _record_for_realtime_execution_attachment(value, runner)
    agent = record.route_pin.agent
    from tools.mcp_tool import _agent_tools_lock

    with _agent_tools_lock:
        tools = getattr(agent, "tools", None)
        valid_names = getattr(agent, "valid_tool_names", None)
        projected = _project_realtime_tool_definitions(tools, valid_names)
    # A route/agent/lease rotation that wins while the bounded projection runs
    # invalidates the result. A later rotation linearizes after this call.
    _record_for_realtime_execution_attachment(value, runner)
    return projected


def _unsafe_tool_schema() -> RealtimeVoiceInvocationError:
    # Never interpolate attacker-controlled schema values into the exception.
    return RealtimeVoiceInvocationError("live agent tool schema is unsafe")


def _claim_schema_node(value: object, *, depth: int, budget: list[int]) -> None:
    if depth > _MAX_TOOL_SCHEMA_DEPTH:
        raise _unsafe_tool_schema()
    budget[0] += 1
    if budget[0] > _MAX_TOOL_SCHEMA_NODES:
        raise _unsafe_tool_schema()
    if type(value) in (dict, list) and len(value) > _MAX_TOOL_SCHEMA_NODES - budget[0]:
        raise _unsafe_tool_schema()


def _encoded_schema_string(value: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _unsafe_tool_schema() from exc


def _account_discarded_string(value: str, byte_budget: list[int]) -> None:
    remaining = _MAX_DISCARDED_TOOL_SCHEMA_BYTES - byte_budget[0]
    # Every Unicode scalar requires at least one UTF-8 byte.  Reject impossible
    # fits before allocating an encoded copy of an attacker-sized string.
    if len(value) > remaining:
        raise _unsafe_tool_schema()
    byte_budget[0] += len(_encoded_schema_string(value))
    if byte_budget[0] > _MAX_DISCARDED_TOOL_SCHEMA_BYTES:
        raise _unsafe_tool_schema()


def _account_discarded_json(
    value: object,
    *,
    depth: int,
    budget: list[int],
    byte_budget: list[int],
) -> None:
    """Validate and bound an omitted subtree without retaining or copying it."""

    _claim_schema_node(value, depth=depth, budget=budget)
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise _unsafe_tool_schema()
            remaining = _MAX_DISCARDED_TOOL_SCHEMA_BYTES - byte_budget[0]
            if len(key) > 512 or len(key) > remaining:
                raise _unsafe_tool_schema()
            encoded_key = _encoded_schema_string(key)
            if len(encoded_key) > 512:
                raise _unsafe_tool_schema()
            byte_budget[0] += len(encoded_key)
            if byte_budget[0] > _MAX_DISCARDED_TOOL_SCHEMA_BYTES:
                raise _unsafe_tool_schema()
            _account_discarded_json(
                item,
                depth=depth + 1,
                budget=budget,
                byte_budget=byte_budget,
            )
        return
    if type(value) is list:
        for item in value:
            _account_discarded_json(
                item,
                depth=depth + 1,
                budget=budget,
                byte_budget=byte_budget,
            )
        return
    if type(value) is str:
        _account_discarded_string(value, byte_budget)
        return
    if value is None or type(value) is bool or type(value) is int:
        return
    if type(value) is float and math.isfinite(value):
        return
    raise _unsafe_tool_schema()


def _copy_bounded_json(
    value: object,
    *,
    depth: int,
    budget: list[int],
    discarded_bytes: list[int],
) -> object:
    _claim_schema_node(value, depth=depth, budget=budget)
    if type(value) is dict:
        copied: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise _unsafe_tool_schema()
            if key == "default":
                _account_discarded_json(
                    item,
                    depth=depth + 1,
                    budget=budget,
                    byte_budget=discarded_bytes,
                )
                continue
            if len(_encoded_schema_string(key)) > 512:
                raise _unsafe_tool_schema()
            copied[key] = _copy_bounded_json(
                item,
                depth=depth + 1,
                budget=budget,
                discarded_bytes=discarded_bytes,
            )
        return copied
    if type(value) is list:
        return [
            _copy_bounded_json(
                item,
                depth=depth + 1,
                budget=budget,
                discarded_bytes=discarded_bytes,
            )
            for item in value
        ]
    if type(value) is str:
        _encoded_schema_string(value)
        return value
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise _unsafe_tool_schema()


def _project_realtime_tool_definitions(
    tools: object, valid_names: object
) -> list[dict[str, object]]:
    if type(tools) is not list or len(tools) > _MAX_REALTIME_TOOLS:
        raise _unsafe_tool_schema()
    if type(valid_names) is not set or any(type(name) is not str for name in valid_names):
        raise _unsafe_tool_schema()
    projected: list[dict[str, object]] = []
    names: set[str] = set()
    for tool in tools:
        if type(tool) is not dict or tool.get("type") != "function":
            raise _unsafe_tool_schema()
        function = tool.get("function")
        if type(function) is not dict:
            raise _unsafe_tool_schema()
        name = function.get("name")
        description = function.get("description")
        parameters = function.get("parameters")
        if (
            type(name) is not str
            or _TOOL_NAME_RE.fullmatch(name) is None
            or name in names
            or name not in valid_names
            or type(description) is not str
            or type(parameters) is not dict
            or parameters.get("type") != "object"
        ):
            raise _unsafe_tool_schema()
        try:
            description_bytes = description.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _unsafe_tool_schema() from exc
        if len(description_bytes) > _MAX_TOOL_DESCRIPTION_BYTES or any(
            ord(char) < 32 and ord(char) not in (9, 10, 13)
            for char in description
        ):
            raise _unsafe_tool_schema()
        copied_parameters = _copy_bounded_json(
            parameters,
            depth=0,
            budget=[0],
            discarded_bytes=[0],
        )
        projected.append(
            {
                "name": name,
                "description": description,
                "parameters": copied_parameters,
            }
        )
        names.add(name)
    if names != valid_names:
        raise _unsafe_tool_schema()
    try:
        encoded = json.dumps(
            projected,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _unsafe_tool_schema() from exc
    if len(encoded) > _MAX_TOOL_DEFINITIONS_BYTES:
        raise _unsafe_tool_schema()
    return projected


def _invalid_tool_call_permit() -> RealtimeVoiceInvocationError:
    return RealtimeVoiceInvocationError("provider tool call permit input is invalid")


def _normalized_provider_id(value: object) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise _invalid_tool_call_permit()
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
        raise _invalid_tool_call_permit()
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _invalid_tool_call_permit() from exc
    if len(encoded) > _MAX_PROVIDER_ID_BYTES:
        raise _invalid_tool_call_permit()
    return value


def _validate_tool_arguments(
    value: object,
    *,
    depth: int,
    counts: list[int],
) -> None:
    if depth > _MAX_TOOL_ARGUMENT_DEPTH:
        raise _invalid_tool_call_permit()
    counts[0] += 1
    if counts[0] > _MAX_TOOL_ARGUMENT_NODES:
        raise _invalid_tool_call_permit()
    if type(value) is dict:
        counts[1] += len(value)
        if counts[1] > _MAX_TOOL_ARGUMENT_KEYS:
            raise _invalid_tool_call_permit()
        for key, item in value.items():
            if type(key) is not str or len(key) > _MAX_TOOL_ARGUMENT_BYTES:
                raise _invalid_tool_call_permit()
            try:
                encoded_key = key.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise _invalid_tool_call_permit() from exc
            if len(encoded_key) > _MAX_TOOL_ARGUMENT_KEY_BYTES:
                raise _invalid_tool_call_permit()
            _validate_tool_arguments(item, depth=depth + 1, counts=counts)
        return
    if type(value) is list:
        if len(value) > _MAX_TOOL_ARGUMENT_NODES - counts[0]:
            raise _invalid_tool_call_permit()
        for item in value:
            _validate_tool_arguments(item, depth=depth + 1, counts=counts)
        return
    if type(value) is str:
        if len(value) > _MAX_TOOL_ARGUMENT_BYTES:
            raise _invalid_tool_call_permit()
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _invalid_tool_call_permit() from exc
        return
    if value is None or type(value) is bool or type(value) is int:
        return
    if type(value) is float and math.isfinite(value):
        return
    raise _invalid_tool_call_permit()


def _canonical_tool_arguments(value: object) -> bytes:
    if type(value) is not dict:
        raise _invalid_tool_call_permit()
    _validate_tool_arguments(value, depth=0, counts=[0, 0])
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _invalid_tool_call_permit() from exc
    if len(encoded) > _MAX_TOOL_ARGUMENT_BYTES:
        raise _invalid_tool_call_permit()
    return encoded


def _purge_tool_call_permit_state(state: _AttachmentPermitState) -> None:
    with _state_lock:
        for permit in tuple(state.permits):
            _tool_call_permit_records.pop(permit, None)
        state.permits.clear()
        state.seen_identities.clear()


def _live_tool_surface_for_permit(
    record: _ExecutionAttachmentRecord, tool_name: object
) -> tuple[object, object, object]:
    if type(tool_name) is not str:
        raise _invalid_tool_call_permit()
    agent = record.route_pin.agent
    from tools.mcp_tool import _agent_tools_lock

    with _agent_tools_lock:
        tools = getattr(agent, "tools", None)
        valid_names = getattr(agent, "valid_tool_names", None)
        projected = _project_realtime_tool_definitions(tools, valid_names)
        if tool_name not in {definition["name"] for definition in projected}:
            raise _invalid_tool_call_permit()
        return agent, tools, valid_names


def _mint_tool_call_permit_for_realtime_execution_attachment(
    value: object,
    *,
    response_id: object,
    item_id: object,
    call_id: object,
    batch_id: object,
    tool_name: object,
    arguments: object,
) -> object:
    """Mint a bounded opaque receipt without dispatching or executing a tool."""

    with _state_lock:
        attachment_record = _execution_attachment_records.get(value)
    runner = attachment_record.runner_ref() if attachment_record is not None else None
    if runner is None:
        raise RealtimeVoiceInvocationError(
            "realtime execution attachment authority changed"
        )
    attachment_record = _record_for_realtime_execution_attachment(value, runner)
    normalized_ids = (
        _normalized_provider_id(response_id),
        _normalized_provider_id(item_id),
        _normalized_provider_id(call_id),
        _normalized_provider_id(batch_id),
    )
    arguments_bytes = _canonical_tool_arguments(arguments)
    _live_tool_surface_for_permit(attachment_record, tool_name)

    from gateway.realtime_execution_attachment import _mint_realtime_tool_call_permit

    with _state_lock:
        if _execution_attachment_records.get(value) is not attachment_record:
            raise RealtimeVoiceInvocationError(
                "realtime execution attachment authority changed"
            )
        current = _record_for_realtime_execution_attachment(value, runner)
        agent, tools, valid_names = _live_tool_surface_for_permit(current, tool_name)
        state = _attachment_permit_states.get(value)
        if state is None:
            state = _AttachmentPermitState(set(), weakref.WeakSet())
            state.finalizer = weakref.finalize(
                value, _purge_tool_call_permit_state, state
            )
            _attachment_permit_states[value] = state
        if normalized_ids in state.seen_identities:
            raise RealtimeVoiceInvocationError(
                "provider tool call identity was already admitted"
            )
        if len(state.permits) >= _MAX_OUTSTANDING_TOOL_CALL_PERMITS:
            raise RealtimeVoiceInvocationError(
                "provider tool call permit capacity is exhausted"
            )
        permit = _mint_realtime_tool_call_permit()
        permit_record = _ToolCallPermitRecord(
            attachment_ref=weakref.ref(value),
            attachment_record=current,
            route_pin=current.route_pin,
            agent=agent,
            tool_surface=tools,
            valid_tool_names=valid_names,
            response_id=normalized_ids[0],
            item_id=normalized_ids[1],
            call_id=normalized_ids[2],
            batch_id=normalized_ids[3],
            tool_name=tool_name,
            arguments_bytes=arguments_bytes,
            arguments_digest=hashlib.sha256(arguments_bytes).digest(),
            mint_generation=current.route_pin.session_state.persistent.run_generation,
            mint_routing_generation=current.routing_generation,
            mint_high_water=None,
        )
        _tool_call_permit_records[permit] = permit_record
        state.permits.add(permit)
        state.seen_identities.add(normalized_ids)
        return permit


def _record_for_realtime_tool_call_permit(
    attachment: object, permit: object
) -> _ToolCallPermitRecord:
    """Private A1.4 seam: resolve exact ownership without consuming authority."""

    from gateway.realtime_execution_attachment import (
        RealtimeExecutionAttachment,
        RealtimeToolCallPermit,
    )

    if (
        type(attachment) is not RealtimeExecutionAttachment
        or type(permit) is not RealtimeToolCallPermit
    ):
        raise RealtimeVoiceInvocationError("provider tool call permit is unavailable")
    with _state_lock:
        record = _tool_call_permit_records.get(permit)
        state = _attachment_permit_states.get(attachment)
        attachment_record = _execution_attachment_records.get(attachment)
        if (
            record is None
            or state is None
            or permit not in state.permits
            or record.attachment_ref() is not attachment
            or record.attachment_record is not attachment_record
        ):
            raise RealtimeVoiceInvocationError("provider tool call permit is unavailable")
    runner = attachment_record.runner_ref()
    if runner is None:
        raise RealtimeVoiceInvocationError("provider tool call permit is unavailable")
    current = _record_for_realtime_execution_attachment(attachment, runner)
    agent, tools, valid_names = _live_tool_surface_for_permit(current, record.tool_name)
    if (
        current is not record.attachment_record
        or current.route_pin is not record.route_pin
        or agent is not record.agent
        or tools is not record.tool_surface
        or valid_names is not record.valid_tool_names
    ):
        raise RealtimeVoiceInvocationError("provider tool call permit is unavailable")
    return record


def _mint_realtime_execution_receipt_id() -> str:
    """Mint a provider-opaque receipt unrelated to durable row/session IDs."""

    return uuid4().hex


def _provider_ready_tool_output(content: object) -> str:
    """Project canonical tool content to the existing bounded text surface."""

    from agent.message_content import flatten_message_text
    from tools.tool_output_limits import get_max_bytes

    output = flatten_message_text(content)
    encoded = output.encode("utf-8", errors="replace")
    limit = get_max_bytes()
    if len(encoded) <= limit:
        return output
    marker = b"\n...[tool output truncated]"
    if limit <= len(marker):
        return marker[: max(0, limit)].decode("ascii")
    keep = limit - len(marker)
    return (encoded[:keep] + marker).decode("utf-8", errors="ignore")


def _admit_realtime_tool_batch(
    attachment: object, permits: object, route_pin: object
) -> tuple[_ExecutionAttachmentRecord, tuple[_ToolCallPermitRecord, ...], object]:
    """Validate the whole batch and consume it at one host lock boundary."""

    from gateway.external_tool_batch import mint_route_execution_permit
    from gateway.realtime_execution_attachment import (
        RealtimeExecutionAttachment,
        RealtimeToolCallPermit,
    )

    if (
        type(attachment) is not RealtimeExecutionAttachment
        or type(permits) is not tuple
        or not permits
        or len(permits) > _MAX_TOOL_CALL_PERMITS_PER_BATCH
    ):
        raise RealtimeVoiceInvocationError("provider tool call batch is unavailable")
    with _state_lock:
        attachment_record = _execution_attachment_records.get(attachment)
        state = _attachment_permit_states.get(attachment)
        if attachment_record is None or state is None:
            raise RealtimeVoiceInvocationError("provider tool call batch is unavailable")
        if any(type(permit) is not RealtimeToolCallPermit for permit in permits):
            raise RealtimeVoiceInvocationError("provider tool call batch is unavailable")
        if len({id(permit) for permit in permits}) != len(permits):
            raise RealtimeVoiceInvocationError("provider tool call batch is unavailable")

        records: list[_ToolCallPermitRecord] = []
        for permit in permits:
            record = _tool_call_permit_records.get(permit)
            if (
                record is None
                or record.consumed
                or permit not in state.permits
                or record.attachment_ref() is not attachment
                or record.attachment_record is not attachment_record
            ):
                raise RealtimeVoiceInvocationError(
                    "provider tool call batch is unavailable"
                )
            current = _record_for_realtime_tool_call_permit(attachment, permit)
            if current is not record or current.route_pin is not attachment_record.route_pin:
                raise RealtimeVoiceInvocationError(
                    "provider tool call batch is unavailable"
                )
            records.append(record)
        if len({record.call_id for record in records}) != len(records):
            raise RealtimeVoiceInvocationError("provider tool call batch is unavailable")

        if (
            route_pin.agent is not attachment_record.route_pin.agent
            or route_pin.session_entry is not attachment_record.route_pin.session_entry
            or route_pin.session_id != attachment_record.route_pin.session_id
        ):
            raise RealtimeVoiceInvocationError("provider tool call batch is unavailable")
        execution_permit = mint_route_execution_permit(route_pin)
        for record in records:
            record.consumed = True
        return attachment_record, tuple(records), execution_permit


def _execute_admitted_realtime_tool_batch(
    attachment_record: _ExecutionAttachmentRecord,
    route_pin: object,
    records: tuple[_ToolCallPermitRecord, ...],
    execution_permit: object,
    approval_notifier: Callable[[dict], None],
) -> tuple[dict[str, str], ...]:
    """Build one exact canonical batch and project only its durable results."""

    from agent.external_tool_batch import ExternalToolBatchEnvelope
    from gateway.external_tool_batch import execute_route_owned_external_tool_batch

    calls = [
        SimpleNamespace(
            id=record.call_id,
            type="function",
            function=SimpleNamespace(
                name=record.tool_name,
                arguments=record.arguments_bytes.decode("utf-8"),
            ),
        )
        for record in records
    ]
    assistant_message = SimpleNamespace(content="", tool_calls=calls)
    assistant_row = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in calls
        ],
    }
    messages: list[dict[str, Any]] = []
    receipt = execute_route_owned_external_tool_batch(
        pin=route_pin,
        execution_permit=execution_permit,
        assistant_message=assistant_message,
        assistant_row=assistant_row,
        messages=messages,
        envelope=ExternalToolBatchEnvelope(
            task_id=f"realtime-tool-batch-{uuid4().hex}",
            turn_id=uuid4().hex,
            call_ids=tuple(record.call_id for record in records),
        ),
        approval_notifier=approval_notifier,
    )
    outputs = tuple(_provider_ready_tool_output(row[2]) for row in receipt.tool_rows)
    return tuple(
        {
            "call_id": record.call_id,
            "output": output,
            "receipt_id": _mint_realtime_execution_receipt_id(),
        }
        for record, output in zip(records, outputs, strict=True)
    )


async def _execute_tool_batch_for_realtime_execution_attachment(
    attachment: object, permits: object
) -> tuple[dict[str, str], ...]:
    """Acquire a canonical turn, then retain admitted execution through cleanup."""

    with _state_lock:
        attachment_record = _execution_attachment_records.get(attachment)
    runner = attachment_record.runner_ref() if attachment_record is not None else None
    if runner is None:
        raise RealtimeVoiceInvocationError("provider tool call batch is unavailable")
    adapter = attachment_record.adapter_ref()
    if adapter is None:
        raise RealtimeVoiceInvocationError("provider tool call batch is unavailable")
    loop = asyncio.get_running_loop()
    binding = attachment_record.binding
    turn_context = runner._acquire_external_tool_batch_turn(
        attachment_record.route_pin
    )
    route_pin = await turn_context.__aenter__()
    try:
        attachment_record, records, execution_permit = _admit_realtime_tool_batch(
            attachment, permits, route_pin
        )
    except BaseException:
        await turn_context.__aexit__(None, None, None)
        raise

    def notify(approval_data: dict) -> None:
        from gateway import run as gateway_run

        gateway_run._approval_notify_sync(
            approval_data,
            adapter=adapter,
            chat_id=binding.chat_id,
            session_key=route_pin.session_key,
            thread_metadata=(
                {"thread_id": binding.thread_id} if binding.thread_id else None
            ),
            loop=loop,
        )

    async def execute_and_release() -> tuple[dict[str, str], ...]:
        try:
            return await asyncio.to_thread(
                _execute_admitted_realtime_tool_batch,
                attachment_record,
                route_pin,
                records,
                execution_permit,
                notify,
            )
        finally:
            await turn_context.__aexit__(None, None, None)

    owner = asyncio.create_task(execute_and_release())
    try:
        return await asyncio.shield(owner)
    except asyncio.CancelledError:
        owner.add_done_callback(
            lambda task: task.exception() if not task.cancelled() else None
        )
        raise
    except Exception as exc:
        from agent.external_tool_batch import ExternalToolBatchPersistenceError
        from gateway.external_tool_batch import ExternalToolBatchRouteChanged

        if isinstance(exc, ExternalToolBatchPersistenceError):
            raise ExternalToolBatchPersistenceError(
                "canonical tool batch durability proof failed"
            ) from exc
        if isinstance(exc, ExternalToolBatchRouteChanged):
            raise
        raise RealtimeVoiceInvocationError(
            "canonical provider tool batch execution failed"
        ) from exc


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
        permit_state = _attachment_permit_states.pop(value, None)
        if permit_state is not None:
            finalizer = permit_state.finalizer
            if finalizer is not None:
                finalizer.detach()
            _purge_tool_call_permit_state(permit_state)
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
