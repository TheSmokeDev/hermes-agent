"""Exact route ownership for the host external tool-batch operation."""

from collections import OrderedDict
from datetime import datetime, timezone
import threading
from types import SimpleNamespace

import pytest

from gateway.external_tool_batch import (
    _claim_route_execution_permit,
    ExternalToolBatchRouteChanged,
    execute_route_owned_external_tool_batch,
    mint_route_execution_permit,
    pin_route_owned_agent,
    revalidate_route_owned_agent,
    pin_durable_route_owned_agent,
    revalidate_durable_route_owned_agent,
    gateway_approval_context,
)
from agent.external_tool_batch import ExternalToolBatchEnvelope
from gateway.session import SessionEntry
from gateway.session_state import SessionState
from gateway.run import GatewayRunner


def _runner():
    key = "agent:main:discord:dm:chat"
    agent = SimpleNamespace(session_id="durable-1")
    entry = SessionEntry(
        session_key=key,
        session_id="durable-1",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    state = SessionState()
    state.turn.agent = agent
    state.turn.lease = object()
    state.turn.lease_token = object()
    state.turn.lease_generation = 7
    state.persistent.run_generation = 7
    store = SimpleNamespace(
        _lock=threading.RLock(),
        _entries={key: entry},
        _ensure_loaded_locked=lambda: None,
    )
    runner = SimpleNamespace(
        session_store=store,
        _agent_cache_lock=threading.RLock(),
        _agent_cache=OrderedDict({key: (agent, "sig", 0, "durable-1")}),
        _session_states={key: state},
    )
    return runner, key, agent, entry, state


def test_durable_route_agent_binding_survives_idle_turn_clear_and_generation_increment():
    runner, key, agent, entry, state = _runner()
    source = object()
    runner.session_store._route_structural_generations = {key: 3}
    state.turn.clear()

    binding = pin_durable_route_owned_agent(runner, key, source=source)

    assert binding.agent is agent
    assert binding.session_entry is entry
    assert binding.session_state is state
    assert binding.routing_generation == 3
    assert binding.source is source
    revalidate_durable_route_owned_agent(runner, binding)

    state.persistent.run_generation += 1
    revalidate_durable_route_owned_agent(runner, binding)


def test_route_pin_rejects_recreated_field_equivalent_authority():
    runner, key, agent, entry, state = _runner()
    pin = pin_route_owned_agent(runner, key)

    assert pin.agent is agent
    assert pin.session_entry is entry
    assert pin.active_session_lease is state.turn.lease
    assert pin.turn_lease_token is state.turn.lease_token

    revalidate_route_owned_agent(runner, pin)

    lookalike_agent = SimpleNamespace(session_id="durable-1")
    lookalike_entry = SessionEntry(
        session_key=entry.session_key,
        session_id=entry.session_id,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )
    runner.session_store._entries[key] = lookalike_entry
    runner._agent_cache[key] = (lookalike_agent, "sig", 0, "durable-1")
    state.turn.agent = lookalike_agent

    with pytest.raises(ExternalToolBatchRouteChanged):
        revalidate_route_owned_agent(runner, pin)


@pytest.mark.parametrize("mutation", ["remove", "generation", "session", "lease"])
def test_route_pin_fails_closed_on_route_rotation(mutation):
    runner, key, _agent, _entry, state = _runner()
    pin = pin_route_owned_agent(runner, key)

    if mutation == "remove":
        runner._agent_cache.pop(key)
    elif mutation == "generation":
        state.persistent.run_generation += 1
    elif mutation == "session":
        runner.session_store._entries[key].session_id = "durable-2"
    else:
        state.turn.lease_token = object()
    with pytest.raises(ExternalToolBatchRouteChanged):
        revalidate_route_owned_agent(runner, pin)


def test_execution_permit_route_rotation_before_claim_has_zero_persistence_and_dispatch():
    runner, key, agent, _entry, state = _runner()
    persisted = []
    dispatched = []
    agent._session_db = SimpleNamespace(get_messages=lambda _sid: [])
    agent._flush_messages_to_session_db = lambda *_args: persisted.append(True)
    agent._execute_tool_calls = lambda *_args: dispatched.append(True)
    agent._incremental_persistence_failed = False
    pin = pin_route_owned_agent(runner, key)
    permit = mint_route_execution_permit(pin)
    state.turn.lease_token = object()

    with pytest.raises(ExternalToolBatchRouteChanged):
        execute_route_owned_external_tool_batch(
            pin=pin,
            execution_permit=permit,
            assistant_message=SimpleNamespace(tool_calls=[]),
            assistant_row={"role": "assistant", "tool_calls": []},
            messages=[],
            envelope=ExternalToolBatchEnvelope("task", "turn", ()),
            approval_notifier=lambda _data: None,
        )

    assert persisted == []
    assert dispatched == []


def test_execution_permit_claim_linearizes_with_production_lease_release(monkeypatch):
    import gateway.external_tool_batch as batch_host

    runner, key, agent, _entry, state = _runner()
    persisted = []
    dispatched = []
    released = []
    agent._session_db = SimpleNamespace(get_messages=lambda _sid: [])
    agent._flush_messages_to_session_db = lambda *_args: persisted.append(True)
    agent._execute_tool_calls = lambda *_args: dispatched.append(True)
    agent._incremental_persistence_failed = False
    runner._turn_leases = SimpleNamespace(release=lambda token: released.append(token) or True)
    runner._peek_session_state = lambda session_key: runner._session_states.get(session_key)
    pin = pin_route_owned_agent(runner, key)
    permit = mint_route_execution_permit(pin)
    checked = threading.Event()
    resume = threading.Event()
    original_state = batch_host._state
    state_reads = 0

    def barrier_state(current_runner, session_key):
        nonlocal state_reads
        state_reads += 1
        if state_reads == 2:
            checked.set()
            assert resume.wait(2)
        return original_state(current_runner, session_key)

    monkeypatch.setattr(batch_host, "_state", barrier_state)
    errors = []

    def execute():
        try:
            execute_route_owned_external_tool_batch(
                pin=pin,
                execution_permit=permit,
                assistant_message=SimpleNamespace(tool_calls=[]),
                assistant_row={"role": "assistant", "tool_calls": []},
                messages=[],
                envelope=ExternalToolBatchEnvelope("task", "turn", ()),
                approval_notifier=lambda _data: None,
            )
        except Exception as exc:
            errors.append(exc)

    claim_thread = threading.Thread(target=execute)
    claim_thread.start()
    assert checked.wait(2)
    assert GatewayRunner._release_turn_lease(runner, key, 7) is True
    resume.set()
    claim_thread.join(2)

    assert not claim_thread.is_alive()
    assert persisted == []
    assert dispatched == []
    assert len(errors) == 1
    assert isinstance(errors[0], ExternalToolBatchRouteChanged)
    assert released == [pin.turn_lease_token]


def test_execution_permit_claim_linearizes_with_real_generation_writer(monkeypatch):
    import gateway.external_tool_batch as batch_host

    runner, key, _agent, _entry, state = _runner()
    runner._peek_session_state = lambda session_key: runner._session_states.get(session_key)
    runner._session_state = lambda session_key: runner._session_states[session_key]
    pin = pin_route_owned_agent(runner, key)
    permit = mint_route_execution_permit(pin)
    final_read = threading.Event()
    resume = threading.Event()
    original_read = batch_host._current_run_generation

    def barrier_read(current_state):
        final_read.set()
        assert resume.wait(2)
        return original_read(current_state)

    monkeypatch.setattr(batch_host, "_current_run_generation", barrier_read)
    claim_errors = []
    def claim():
        try:
            _claim_route_execution_permit(pin, permit)
        except Exception as exc:
            claim_errors.append(exc)

    claim_thread = threading.Thread(target=claim)
    claim_thread.start()
    assert final_read.wait(2)

    rotated = threading.Event()
    rotation_thread = threading.Thread(
        target=lambda: (
            GatewayRunner._begin_session_run_generation(runner, key),
            rotated.set(),
        )
    )
    rotation_thread.start()
    assert not rotated.wait(0.1)
    resume.set()
    claim_thread.join(2)
    rotation_thread.join(2)

    assert claim_errors == []
    assert permit._claimed is True
    assert state.persistent.run_generation == 8


def test_route_revalidation_linearizes_with_real_generation_writer():
    runner, key, _agent, _entry, state = _runner()
    runner._peek_session_state = lambda session_key: runner._session_states.get(session_key)
    runner._session_state = lambda session_key: runner._session_states[session_key]
    pin = pin_route_owned_agent(runner, key)
    generation_read = threading.Event()
    resume_read = threading.Event()
    validator_thread = None

    class BarrierPersistent:
        def __init__(self, generation):
            self._generation = generation

        @property
        def run_generation(self):
            captured = self._generation
            if threading.current_thread() is validator_thread and not generation_read.is_set():
                generation_read.set()
                assert resume_read.wait(2)
            return captured

        @run_generation.setter
        def run_generation(self, value):
            self._generation = value

    state.persistent = BarrierPersistent(pin.generation)
    validation_errors = []

    def validate():
        try:
            revalidate_route_owned_agent(runner, pin)
        except Exception as exc:
            validation_errors.append(exc)

    validator_thread = threading.Thread(target=validate)
    validator_thread.start()
    assert generation_read.wait(2)

    rotated = threading.Event()
    rotation_thread = threading.Thread(
        target=lambda: (
            GatewayRunner._begin_session_run_generation(runner, key),
            rotated.set(),
        )
    )
    rotation_thread.start()
    assert not rotated.wait(0.1)
    resume_read.set()
    validator_thread.join(2)
    rotation_thread.join(2)

    assert not validator_thread.is_alive()
    assert not rotation_thread.is_alive()
    assert validation_errors == []
    assert rotated.is_set()
    assert state.persistent.run_generation == pin.generation + 1


def test_execution_permit_is_one_use():
    runner, key, _agent, _entry, _state = _runner()
    pin = pin_route_owned_agent(runner, key)
    permit = mint_route_execution_permit(pin)

    _claim_route_execution_permit(pin, permit)
    with pytest.raises(ExternalToolBatchRouteChanged):
        _claim_route_execution_permit(pin, permit)


@pytest.mark.parametrize(
    "mutation",
    ["agent", "entry", "generation", "lease", "lease_generation", "turn_token"],
)
def test_execution_permit_exact_binding_fails_before_effect(mutation):
    runner, key, agent, entry, state = _runner()
    pin = pin_route_owned_agent(runner, key)
    permit = mint_route_execution_permit(pin)
    if mutation == "agent":
        runner._agent_cache[key] = (object(), "sig", 0, "durable-1")
    elif mutation == "entry":
        runner.session_store._entries[key] = SessionEntry(
            session_key=entry.session_key,
            session_id=entry.session_id,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
        )
    elif mutation == "generation":
        state.persistent.run_generation += 1
    elif mutation == "lease":
        state.turn.lease = object()
    elif mutation == "lease_generation":
        state.turn.lease_generation += 1
    else:
        state.turn.lease_token = object()

    with pytest.raises(ExternalToolBatchRouteChanged):
        _claim_route_execution_permit(pin, permit)
    assert agent.session_id == "durable-1"


def test_same_key_approval_context_restores_exact_owner():
    from tools import approval

    key = "same-public-key"
    outer = lambda _data: None
    inner = lambda _data: None
    with gateway_approval_context(key, outer):
        assert approval._gateway_notify_cbs[key] is outer
        with gateway_approval_context(key, inner):
            assert approval._gateway_notify_cbs[key] is inner
        assert approval._gateway_notify_cbs[key] is outer
    assert key not in approval._gateway_notify_cbs


def test_same_key_approval_owner_teardown_only_releases_its_pending_request():
    from tools import approval

    key = "overlapping-owner-key"
    outer_notices = []
    inner_notices = []
    outer_cb = outer_notices.append
    inner_cb = inner_notices.append
    outer = approval.register_gateway_notify(key, outer_cb)
    inner = approval.register_gateway_notify(key, inner_cb)
    outer_entry = approval._ApprovalEntry({"command": "outer"})
    inner_entry = approval._ApprovalEntry({"command": "inner"})
    outer_entry.owner = outer
    inner_entry.owner = inner
    approval._gateway_queues[key] = [outer_entry, inner_entry]
    try:
        approval.unregister_gateway_notify(key, inner)

        assert inner_entry.event.is_set()
        assert not outer_entry.event.is_set()
        assert approval._gateway_queues[key] == [outer_entry]
        assert approval._gateway_notify_cbs[key] is outer_cb
        assert approval._gateway_notify_for_owner(key, outer) is outer_cb
        assert approval._gateway_notify_for_owner(key, inner) is None
    finally:
        approval.unregister_gateway_notify(key)


def test_same_session_crossed_approval_decisions_resolve_exact_opaque_requests():
    from tools import approval

    key = "crossed-owner-key"
    outer = approval.register_gateway_notify(key, lambda _data: None)
    inner = approval.register_gateway_notify(key, lambda _data: None)
    outer_token = approval.set_gateway_notify_owner(outer)
    outer_entry = approval._ApprovalEntry({"command": "outer"})
    approval.reset_gateway_notify_owner(outer_token)
    inner_token = approval.set_gateway_notify_owner(inner)
    inner_entry = approval._ApprovalEntry({"command": "inner"})
    approval.reset_gateway_notify_owner(inner_token)
    approval._gateway_queues[key] = [outer_entry, inner_entry]
    try:
        assert outer_entry.data["request_id"] != inner_entry.data["request_id"]
        assert approval.resolve_gateway_approval(
            key, "deny", request_id=inner_entry.data["request_id"]
        ) == 1
        assert inner_entry.event.is_set()
        assert not outer_entry.event.is_set()
        assert approval.resolve_gateway_approval(
            key, "once", request_id=outer_entry.data["request_id"]
        ) == 1
        assert outer_entry.event.is_set()
        assert approval.resolve_gateway_approval(
            key, "deny", request_id=outer_entry.data["request_id"]
        ) == 0
        assert approval.resolve_gateway_approval(
            "wrong-session", "deny", request_id=inner_entry.data["request_id"]
        ) == 0
    finally:
        approval.unregister_gateway_notify(key)
