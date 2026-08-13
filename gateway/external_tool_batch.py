"""Gateway ownership fence for the canonical external tool-batch operation."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from agent.external_tool_batch import (
    ExternalToolBatchEnvelope,
    ExternalToolBatchReceipt,
    execute_external_tool_batch,
)


class ExternalToolBatchRouteChanged(RuntimeError):
    """The exact live route authority changed after it was pinned."""


@dataclass(frozen=True)
class RouteOwnedAgentPin:
    runner: Any
    session_key: str
    agent: Any
    session_entry: Any
    session_id: str
    generation: int
    active_session_lease: Any
    turn_lease_token: Any


@dataclass(eq=False)
class RouteExecutionPermit:
    """Opaque, consume-once authority for one canonical admission."""

    pin: RouteOwnedAgentPin
    _claimed: bool = field(default=False, init=False, repr=False)


def mint_route_execution_permit(pin: RouteOwnedAgentPin) -> RouteExecutionPermit:
    return RouteExecutionPermit(pin)


def _state(runner: Any, session_key: str) -> Any:
    peek = getattr(runner, "_peek_session_state", None)
    if callable(peek):
        return peek(session_key)
    return getattr(runner, "_session_states", {}).get(session_key)


def _locked_route_snapshot(runner: Any, session_key: str) -> tuple[Any, Any, Any]:
    store = runner.session_store
    # Match the gateway's established cache -> session-store lock order (the
    # cache invalidation/recovery paths already acquire in this direction).
    with runner._agent_cache_lock:  # noqa: SLF001 - authoritative agent lock
        with store._lock:  # noqa: SLF001 - authoritative route identity lock
            store._ensure_loaded_locked()  # noqa: SLF001
            entry = store._entries.get(session_key)  # noqa: SLF001
            cached = runner._agent_cache.get(session_key)  # noqa: SLF001
            state = _state(runner, session_key)
            return entry, cached, state


def pin_route_owned_agent(runner: Any, session_key: str) -> RouteOwnedAgentPin:
    """Acquire and pin exact current route objects under existing host locks."""
    entry, cached, state = _locked_route_snapshot(runner, session_key)
    if (
        entry is None
        or not isinstance(cached, tuple)
        or not cached
        or state is None
        or cached[0] is not state.turn.agent
        or getattr(cached[0], "session_id", None) != entry.session_id
        or state.turn.lease is None
        or state.turn.lease_token is None
        or state.turn.lease_generation != state.persistent.run_generation
    ):
        raise ExternalToolBatchRouteChanged("route has no exact live owned agent")
    return RouteOwnedAgentPin(
        runner=runner,
        session_key=session_key,
        agent=cached[0],
        session_entry=entry,
        session_id=entry.session_id,
        generation=state.persistent.run_generation,
        active_session_lease=state.turn.lease,
        turn_lease_token=state.turn.lease_token,
    )


def revalidate_route_owned_agent(runner: Any, pin: RouteOwnedAgentPin) -> None:
    """Fail closed on removal, recreation, rotation, or equal lookalikes."""
    if runner is not pin.runner:
        raise ExternalToolBatchRouteChanged("runner identity changed")
    entry, cached, state = _locked_route_snapshot(runner, pin.session_key)
    if (
        entry is not pin.session_entry
        or entry is None
        or entry.session_id != pin.session_id
        or not isinstance(cached, tuple)
        or not cached
        or cached[0] is not pin.agent
        or getattr(pin.agent, "session_id", None) != pin.session_id
        or state is None
        or state.turn.agent is not pin.agent
        or state.persistent.run_generation != pin.generation
        or state.turn.lease is not pin.active_session_lease
        or state.turn.lease_token is not pin.turn_lease_token
        or state.turn.lease_generation != pin.generation
    ):
        raise ExternalToolBatchRouteChanged("route authority changed")


def _claim_route_execution_permit(pin: RouteOwnedAgentPin, permit: RouteExecutionPermit) -> None:
    if type(permit) is not RouteExecutionPermit or permit.pin is not pin:
        raise ExternalToolBatchRouteChanged("execution permit ownership changed")
    runner = pin.runner
    store = runner.session_store
    with runner._agent_cache_lock:
        with store._lock:
            if permit._claimed:
                raise ExternalToolBatchRouteChanged("execution permit already consumed")
            store._ensure_loaded_locked()
            entry = store._entries.get(pin.session_key)
            cached = runner._agent_cache.get(pin.session_key)
            state = _state(runner, pin.session_key)
            if (
                entry is not pin.session_entry
                or entry is None
                or entry.session_id != pin.session_id
                or not isinstance(cached, tuple)
                or not cached
                or cached[0] is not pin.agent
                or state is None
                or state.turn.agent is not pin.agent
                or state.persistent.run_generation != pin.generation
                or state.turn.lease is not pin.active_session_lease
                or state.turn.lease_token is not pin.turn_lease_token
                or state.turn.lease_generation != pin.generation
            ):
                raise ExternalToolBatchRouteChanged("route authority changed")
            permit._claimed = True


@contextmanager
def gateway_approval_context(
    session_key: str,
    notifier: Callable[[dict], None],
) -> Iterator[None]:
    """Install the existing approval session/notifier for one host operation."""
    from tools.approval import (
        register_gateway_notify,
        reset_gateway_notify_owner,
        reset_current_session_key,
        set_gateway_notify_owner,
        set_current_session_key,
        unregister_gateway_notify,
    )

    token = set_current_session_key(session_key)
    owner = register_gateway_notify(session_key, notifier)
    owner_token = set_gateway_notify_owner(owner)
    try:
        yield
    finally:
        try:
            unregister_gateway_notify(session_key, owner)
        finally:
            reset_gateway_notify_owner(owner_token)
            reset_current_session_key(token)


def execute_route_owned_external_tool_batch(
    *,
    pin: RouteOwnedAgentPin,
    execution_permit: RouteExecutionPermit,
    assistant_message: Any,
    assistant_row: dict[str, Any],
    messages: list[dict[str, Any]],
    envelope: ExternalToolBatchEnvelope,
    approval_notifier: Callable[[dict], None],
) -> ExternalToolBatchReceipt:
    """Atomically claim current route authority, then invoke canonical operation."""
    _claim_route_execution_permit(pin, execution_permit)
    with gateway_approval_context(pin.session_key, approval_notifier):
        receipt = execute_external_tool_batch(
            agent=pin.agent,
            assistant_message=assistant_message,
            assistant_row=assistant_row,
            messages=messages,
            envelope=envelope,
        )
    assert receipt is not None
    return receipt
