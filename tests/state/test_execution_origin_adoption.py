"""Atomic execution-input ownership, passive races and durable adoption receipts."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import sqlite3
from threading import Barrier

import pytest

from hermes_state import SessionDB
from hermes_state_execution_origins import origin_metadata
from hermes_state_passive_history import PassiveHistoryConflictError, PassiveHistoryRetiredError
from passive_history_ingress import PRODUCER

TEXT = "original spoken request"
IDENTITY = {"producer": PRODUCER, "event_id": "event", "origin_turn_id": "utterance"}


@pytest.fixture
def stores(tmp_path):
    first, second = SessionDB(tmp_path / "state.db"), SessionDB(tmp_path / "state.db")
    first.create_session("conversation", source="test")
    try:
        yield first, second
    finally:
        first.close()
        second.close()


def reserve(db, **overrides):
    return db.reserve_execution_origin("conversation", **{
        **IDENTITY, "content": TEXT, "run_id": "run_owned", "run_scope": "authenticated-scope",
        **overrides})


def row(claim):
    return {"role": "user", "content": TEXT, "api_content": "  original wire payload\n",
            "platform_message_id": "origin:event",
            "display_metadata": {"execution_origin": origin_metadata(claim)}}


def adopt(db, **overrides):
    return db.adopt_execution_origin("conversation", **{**IDENTITY, "content": TEXT, **overrides})


def test_pending_binds_once_and_retries_keep_original_cached_bytes(stores):
    db, reader = stores
    claim = reserve(db)
    assert adopt(reader)["status"] == "pending"
    assert reader.get_messages("conversation") == []
    with pytest.raises(PassiveHistoryConflictError):
        db.append_passive_messages("conversation", **IDENTITY, messages=[{"role": "user", "content": TEXT}])
    message = row(claim)
    assert db.append_messages_batch("conversation", [message], _execution_origin=claim) == 1
    proof = adopt(reader)
    assert proof["status"] == "adopted" and proof["run_id"] == "run_owned"
    retry = row(claim)
    retry["api_content"] = "different retry-side wire bytes"
    assert db.append_messages_batch("conversation", [retry], _execution_origin=claim) == 0
    assert retry["_row_id"] == message["_row_id"] == proof["message_ids"][0]
    stored = reader.get_messages("conversation")
    assert len(stored) == 1 and stored[0]["api_content"] == "  original wire payload\n"
    assert reader.get_passive_history_watermark("conversation").revision == 0


def test_passive_and_authoritative_admission_have_one_winner(stores):
    first, second = stores
    gate = Barrier(2)

    def authoritative():
        gate.wait(timeout=5)
        return reserve(first)

    def passive():
        gate.wait(timeout=5)
        return second.append_passive_messages("conversation", **IDENTITY,
                                             messages=[{"role": "user", "content": TEXT}])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(authoritative), pool.submit(passive)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=10))
            except PassiveHistoryConflictError:
                outcomes.append(None)
    assert sum(result is not None for result in outcomes) == 1
    if outcomes[0] is not None:
        first.append_messages_batch("conversation", [row(outcomes[0])], _execution_origin=outcomes[0])
    assert len(second.get_messages("conversation")) == 1


def test_binding_rollback_never_claims_uncommitted_input(stores, monkeypatch):
    db, reader = stores
    claim = reserve(db)
    bind = db._bind_execution_origin_row

    def fail(*args):
        raise sqlite3.OperationalError("injected binding failure")

    with monkeypatch.context() as patch:
        patch.setattr(db, "_bind_execution_origin_row", fail)
        with pytest.raises(sqlite3.OperationalError):
            db.append_messages_batch("conversation", [row(claim)], _execution_origin=claim)
    assert adopt(reader)["status"] == "pending"
    assert reader.get_messages("conversation") == []
    assert db._bind_execution_origin_row == bind
    db.append_messages_batch("conversation", [row(claim)], _execution_origin=claim)
    assert adopt(reader)["status"] == "adopted"


@pytest.mark.parametrize("replaced_claim", [False, True])
def test_deferred_flush_retains_its_origin_after_executor_claim_ends(stores, monkeypatch, replaced_claim):
    from run_agent import AIAgent
    db, reader = stores
    claim = reserve(db)
    agent = AIAgent.__new__(AIAgent)
    agent.session_id, agent._session_db = "conversation", db
    agent._session_db_created, agent._persist_disabled = True, False
    agent._last_flushed_db_idx = 0
    agent._execution_origin_claim = claim
    messages = [row(claim)]
    with monkeypatch.context() as failing:
        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError("injected unavailable first flush")
        failing.setattr(db, "append_messages_batch", unavailable)
        assert agent._flush_messages_to_session_db(messages) is False
    if replaced_claim:
        db.create_session("foreign", source="test")
        agent._execution_origin_claim = db.reserve_execution_origin(
            "foreign", **{**IDENTITY, "event_id": "other-event"}, content=TEXT,
            run_id="other-run", run_scope="other-scope")
    else:
        del agent._execution_origin_claim
    assert agent._flush_messages_to_session_db(messages) is True
    assert adopt(reader)["status"] == "adopted"
    assert len(reader.get_messages("conversation")) == 1
    assert agent._flush_messages_to_session_db([{"role": "user", "content": "unrelated typed input"}]) is True


def test_text_or_fabricated_provenance_is_not_adoption(stores):
    from run_agent import AIAgent
    db, reader = stores
    claim = reserve(db)
    agent = AIAgent.__new__(AIAgent)
    agent.session_id, agent._session_db = "conversation", db
    agent._session_db_created, agent._persist_disabled, agent._last_flushed_db_idx = True, False, 0
    # JSON-like caller data cannot manufacture the typed host-created flush unit.
    fabricated = {**row(claim), "_execution_origin_flush_claim": dict(claim)}
    assert agent._flush_messages_to_session_db([fabricated]) is True
    assert adopt(reader)["status"] == "pending"
    for override in ({"content": "derived worker prompt"}, {"run_id": "another_run"},
                     {"run_scope": "foreign-owner"}):
        with pytest.raises(PassiveHistoryConflictError):
            reserve(db, **override)
    invalid = {**row(claim), "role": "assistant"}
    with pytest.raises(PassiveHistoryConflictError):
        db.append_messages_batch("conversation", [invalid], _execution_origin=claim)
    assert len(reader.get_messages("conversation")) == 1


@pytest.mark.parametrize("pending", [False, True])
def test_deleted_origins_are_tombstones_after_id_recreation(stores, pending):
    db, reader = stores
    claim = reserve(db)
    if not pending:
        db.append_messages_batch("conversation", [row(claim)], _execution_origin=claim)
    db.delete_session("conversation")
    db.create_session("conversation", source="test")
    with pytest.raises(PassiveHistoryRetiredError):
        adopt(reader)
    with pytest.raises(PassiveHistoryRetiredError):
        reserve(reader)
    with pytest.raises(PassiveHistoryConflictError):
        db.append_passive_messages("conversation", **IDENTITY, messages=[{"role": "user", "content": TEXT}])
    assert db.get_messages("conversation") == []


def test_compression_and_branch_ownership(stores):
    db, reader = stores
    claim = reserve(db)
    db.end_session("conversation", "compression")
    db.create_session("tip", source="test", parent_session_id="conversation")
    db.create_session("branch", source="test", parent_session_id="conversation",
                      model_config={"_branched_from": "conversation"})
    with pytest.raises(PassiveHistoryConflictError):
        db.append_messages_batch("branch", [row(claim)], _execution_origin=claim)
    db.append_messages_batch("tip", [row(claim)], _execution_origin=claim)
    proof = adopt(reader)
    assert proof["session_id"] == "tip"
    assert reader.adopt_execution_origin("tip", **IDENTITY, content=TEXT) == proof
    assert reader.get_messages("branch") == []
    # Canonical deletion destroys the original authorization lineage even if the bound row
    # survives in an orphaned child; keeping that old origin live would silently retarget it.
    db.delete_session("conversation")
    assert reader.get_messages("tip")[0]["content"] == TEXT
    assert reader.get_session("tip")["parent_session_id"] is None
    with pytest.raises(PassiveHistoryRetiredError):
        reader.adopt_execution_origin("tip", **IDENTITY, content=TEXT)


def test_recovery_keeps_bound_ids_and_retired_origins(tmp_path):
    from hermes_cli.session_recovery import recover_session_database
    source, output = tmp_path / "source.db", tmp_path / "recovered.db"
    db = SessionDB(source)
    db.create_session("conversation", source="test")
    claim = reserve(db)
    db.append_messages_batch("conversation", [row(claim)], _execution_origin=claim)
    proof = deepcopy(adopt(db))
    db.close()
    report = recover_session_database(source, output, work_dir=tmp_path)
    assert report["copy"]["execution_origins"]["copied_rows"] == 1
    recovered = SessionDB(output)
    try:
        assert adopt(recovered) == proof
        recovered.clear_messages("conversation")
        with pytest.raises(PassiveHistoryRetiredError):
            adopt(recovered)
    finally:
        recovered.close()
