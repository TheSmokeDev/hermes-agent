"""Restore keeps durable user identity; provider copies repair user alternation."""

import copy

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "test_state.db")
    yield session_db
    session_db.close()


def _seed_wedged_session(db, session_id="s1"):
    db.create_session(session_id, "acp")
    db.append_message(session_id, "user", "first ask")
    db.append_message(session_id, "assistant", "first reply")
    db.append_message(session_id, "user", "unanswered turn", api_content="unanswered wire context")
    db.append_message(session_id, "user", "next turn")
    db.append_message(session_id, "assistant", "next reply")


def _provider_messages(history):
    from agent.agent_runtime_helpers import drop_thinking_only_and_merge_users
    from agent.turn_context import build_api_messages
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent.api_mode = "chat_completions"
    agent.provider = "fixture"
    agent.model = "fixture"
    agent.ephemeral_system_prompt = None
    agent._needs_thinking_reasoning_pad = lambda: False
    messages, _ = build_api_messages(
        agent, history, current_turn_user_idx=-1, ext_prefetch_cache=None,
        plugin_user_context=None, moa_config=None, active_system_prompt=None,
    )
    return drop_thinking_only_and_merge_users(messages)


def test_restore_preserves_user_rows_and_repairs_the_provider_copy(db):
    _seed_wedged_session(db)
    stored = db.get_messages("s1")
    messages = db.get_messages_as_conversation("s1", repair_alternation=True, include_row_ids=True)
    before = copy.deepcopy(messages)
    assert [m["_row_id"] for m in messages] == [row["id"] for row in stored]
    assert [m["content"] for m in messages] == [row["content"] for row in stored]
    assert all(m["_db_persisted"] for m in messages)
    assert messages[2]["api_content"] == "unanswered wire context"

    wire = _provider_messages(messages)
    assert [m["role"] for m in wire] == ["user", "assistant", "user", "assistant"]
    assert wire[2]["content"] == "unanswered wire context\n\nnext turn"
    assert all("_row_id" not in m and "api_content" not in m for m in wire)
    assert messages == before
    assert db.get_messages("s1") == stored


def test_repaired_load_is_stable_under_prerequest_repair(db):
    from agent.agent_runtime_helpers import repair_message_sequence

    _seed_wedged_session(db)
    messages = db.get_messages_as_conversation("s1", repair_alternation=True)
    before = copy.deepcopy(messages)
    assert repair_message_sequence(None, messages) == 0
    assert messages == before
    wire = _provider_messages(messages)
    assert repair_message_sequence(None, wire) == 0
    assert messages == before


def test_acp_restore_preserves_durable_rows_for_provider_replay(db):
    from acp_adapter.session import SessionManager

    _seed_wedged_session(db, "acp1")
    stored = db.get_messages("acp1")

    class _StubAgent:
        model = "stub"

    mgr = SessionManager(agent_factory=lambda: _StubAgent(), db=db)
    state = mgr._restore("acp1")
    assert state is not None
    assert [m["content"] for m in state.history] == [row["content"] for row in stored]
    assert all(m["_db_persisted"] for m in state.history)
    before = copy.deepcopy(state.history)
    wire = _provider_messages(state.history)
    assert [m["role"] for m in wire] == ["user", "assistant", "user", "assistant"]
    assert wire[2]["content"] == "unanswered wire context\n\nnext turn"
    assert state.history == before
    assert db.get_messages("acp1") == stored
