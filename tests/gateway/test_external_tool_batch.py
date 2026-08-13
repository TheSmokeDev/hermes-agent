"""Exact route ownership for the host external tool-batch operation."""

from collections import OrderedDict
from datetime import datetime, timezone
import threading
from types import SimpleNamespace

import pytest

from gateway.external_tool_batch import (
    ExternalToolBatchRouteChanged,
    pin_route_owned_agent,
    revalidate_route_owned_agent,
)
from gateway.session import SessionEntry
from gateway.session_state import SessionState


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
