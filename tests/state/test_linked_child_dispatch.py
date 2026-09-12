"""Canonical parent-input reuse and durable at-most-once child dispatch fencing."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

from hermes_state import SessionDB
from hermes_state_passive_history import PassiveHistoryConflictError, PassiveHistoryRetiredError
from passive_history_ingress import PRODUCER

INPUT = "Original user utterance"
ORIGIN = {"producer": PRODUCER, "event_id": "voice-event", "origin_turn_id": "voice-origin"}


@pytest.fixture
def stores(tmp_path):
    first, second = SessionDB(tmp_path / "state.db"), SessionDB(tmp_path / "state.db")
    first.create_session("parent", source="test")
    try:
        yield first, second
    finally:
        first.close()
        second.close()


def prepare(db, **kwargs):
    return db.prepare_child_dispatch("parent", **{
        **ORIGIN, "content": INPUT, "run_id": "run_one", "run_scope": "owner",
        "correlation_id": "action-one", "fingerprint": "full-action-fingerprint", **kwargs})


def test_fresh_input_and_two_distinct_actions_have_one_parent_row(stores):
    db, reader = stores
    first = prepare(db)
    rows = deepcopy(reader.get_messages("parent"))
    assert [(row["role"], row["content"]) for row in rows] == [("user", INPUT)]
    second = prepare(db, run_id="run_two", correlation_id="action-two", fingerprint="other-action")
    assert first["parent_message_id"] == second["parent_message_id"] == rows[0]["id"]
    assert reader.get_messages("parent") == rows
    db.claim_child_dispatch(first)
    with pytest.raises(PassiveHistoryConflictError):
        reader.claim_child_dispatch(first)
    db.claim_child_dispatch(second)


@pytest.mark.parametrize("pair", [False, True])
def test_existing_passive_receipt_is_exact_and_unchanged(stores, pair):
    db, reader = stores
    messages = [{"role": "user", "content": INPUT}]
    if pair:
        messages.append({"role": "assistant", "content": "Spoken reply"})
    receipt = db.append_passive_messages("parent", **ORIGIN, messages=messages)
    before = deepcopy(reader.get_messages("parent"))
    dispatch = prepare(db, receipt_id=receipt.revision)
    assert dispatch["parent_message_id"] == receipt.message_ids[0]
    assert reader.get_messages("parent") == before
    for override in ({"content": "Changed utterance"}, {"origin_turn_id": "foreign-origin"},
                     {"receipt_id": receipt.revision + 1}):
        with pytest.raises(PassiveHistoryConflictError):
            prepare(db, run_id="bad-run", correlation_id="bad-action", fingerprint="bad", **override)
    db.create_session("branch", source="test", parent_session_id="parent", model_config={"_branched_from": "parent"})
    with pytest.raises(PassiveHistoryConflictError):
        db.prepare_child_dispatch("branch", **ORIGIN, content=INPUT, run_id="foreign",
                                  run_scope="owner", correlation_id="foreign", fingerprint="foreign", receipt_id=receipt.revision)
    db.clear_messages("parent")
    with pytest.raises(PassiveHistoryRetiredError):
        prepare(db, run_id="after-delete", correlation_id="after-delete", fingerprint="after-delete", receipt_id=receipt.revision)
    assert db.get_messages("parent") == []


def test_reuse_only_miss_never_creates_an_utterance(stores):
    db, _ = stores
    with pytest.raises(PassiveHistoryConflictError):
        prepare(db, receipt_id=123)
    assert db.get_messages("parent") == []


def test_concurrent_correlation_has_one_dispatch_and_one_user(stores):
    first, second = stores
    gate = Barrier(2)
    def request(db, run_id):
        gate.wait(timeout=5)
        return prepare(db, run_id=run_id)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = [executor.submit(request, first, "run_a"), executor.submit(request, second, "run_b")]
        winners = []
        for future in pending:
            try:
                winners.append(future.result(timeout=10))
            except PassiveHistoryConflictError:
                pass
    assert len(winners) == 1
    assert len(first.get_messages("parent")) == 1
    first.claim_child_dispatch(winners[0])
    # A new connection/process has no authority to replace an uncertain launch.
    restarted = SessionDB(first.db_path)
    try:
        with pytest.raises(PassiveHistoryConflictError):
            restarted.claim_child_dispatch(winners[0])
        with pytest.raises(PassiveHistoryConflictError):
            prepare(restarted, run_id="replacement")
    finally:
        restarted.close()


def test_deleted_lineage_cannot_be_rebound_to_a_recreated_parent(stores):
    db, _ = stores
    db.end_session("parent", "compression")
    db.create_session("tip", source="test", parent_session_id="parent")
    receipt = db.append_passive_messages("parent", **ORIGIN, messages=[{"role": "user", "content": INPUT}])
    prepare(db, receipt_id=receipt.revision)
    db.delete_session("parent")
    db.create_session("parent", source="test")
    assert db.get_messages("tip")[0]["content"] == INPUT
    with pytest.raises(PassiveHistoryConflictError):
        prepare(db, run_id="new-run", correlation_id="new-action", fingerprint="new", receipt_id=receipt.revision)
    assert db.get_messages("parent") == []
