"""Canonical host-owned external tool-batch operation contracts."""

from types import SimpleNamespace
import threading
from unittest.mock import MagicMock

import pytest

from agent.external_tool_batch import (
    ExternalToolBatchEnvelope,
    ExternalToolBatchPersistenceError,
    execute_external_tool_batch,
    get_external_tool_batch_marker,
)


def _call(call_id: str = "call-1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name="web_search", arguments='{"query":"x"}'),
    )


def test_external_tool_batch_persists_executes_and_reads_back_exact_result():
    call = _call()
    assistant = SimpleNamespace(content="", tool_calls=[call])
    assistant_row = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call.id,
            "type": "function",
            "function": {"name": "web_search", "arguments": call.function.arguments},
        }],
    }
    messages = []
    durable_rows = []
    events = []

    def flush(current, conversation_history=None):
        events.append(("persist", current[-1]["role"]))
        for message in current:
            if not message.get("_test_persisted"):
                durable_rows.append({"id": len(durable_rows) + 1, **message})
                message["_test_persisted"] = True
        return True

    agent = SimpleNamespace(
        session_id="durable-session",
        _session_db=SimpleNamespace(get_messages=lambda sid: list(durable_rows)),
        _flush_messages_to_session_db=MagicMock(side_effect=flush),
        _execute_tool_calls=MagicMock(),
        _incremental_persistence_failed=False,
    )

    def canonical_execute(msg, current, task_id, api_call_count=0):
        assert durable_rows[-1]["role"] == "assistant"
        events.append(("effect", msg.tool_calls[0].id))
        current.append({
            "role": "tool",
            "name": "web_search",
            "tool_call_id": call.id,
            "content": "exact result",
            "display_metadata": {
                "external_tool_batch_marker": get_external_tool_batch_marker()
            },
        })
        flush(current)

    agent._execute_tool_calls.side_effect = canonical_execute

    receipt = execute_external_tool_batch(
        agent=agent,
        assistant_message=assistant,
        assistant_row=assistant_row,
        messages=messages,
        envelope=ExternalToolBatchEnvelope(
            task_id="task-1", turn_id="turn-1", call_ids=(call.id,)
        ),
    )

    assert events == [
        ("persist", "assistant"),
        ("effect", call.id),
        ("persist", "tool"),
    ]
    agent._execute_tool_calls.assert_called_once_with(assistant, messages, "task-1", 0)
    assert receipt.session_id == "durable-session"
    assert receipt.turn_id == "turn-1"
    assert receipt.assistant_row_id == 1
    assert receipt.high_water_row_id == 2
    assert receipt.tool_rows == ((2, call.id, "exact result"),)


def test_external_stale_persisted_marker_cannot_authorize_effect():
    assistant_row = {
        "role": "assistant",
        "tool_calls": [{"id": "call-1"}],
        "_db_persisted": True,
    }
    effects = []
    agent = SimpleNamespace(
        session_id="durable-session",
        _session_db=SimpleNamespace(get_messages=lambda _sid: []),
        _flush_messages_to_session_db=lambda *_args: True,
        _execute_tool_calls=lambda *_args: effects.append(True),
        _incremental_persistence_failed=False,
    )

    with pytest.raises(ExternalToolBatchPersistenceError):
        execute_external_tool_batch(
            agent=agent,
            assistant_message=SimpleNamespace(tool_calls=[]),
            assistant_row=assistant_row,
            messages=[],
            envelope=ExternalToolBatchEnvelope("task", "turn", ("call-1",)),
        )

    assert effects == []


@pytest.mark.parametrize("persisted_role", ["assistant", "tool"])
def test_wrong_marked_row_cannot_authorize_effect(persisted_role):
    durable_rows = []
    effects = []

    def flush(messages, _history=None):
        marker = messages[0]["display_metadata"]["external_tool_batch_marker"]
        durable_rows.extend([
            {"id": 1, "role": persisted_role, "tool_call_id": "call-1",
             "tool_calls": [{"id": "wrong-call"}],
             "display_metadata": {"external_tool_batch_marker": marker}},
            {"id": 2, "role": "assistant", "tool_calls": [{"id": "call-1"}]},
        ])
        return True

    agent = SimpleNamespace(
        session_id="durable-session",
        _session_db=SimpleNamespace(get_messages=lambda _sid: list(durable_rows)),
        _flush_messages_to_session_db=flush,
        _execute_tool_calls=lambda *_args: effects.append(True),
        _incremental_persistence_failed=False,
    )
    with pytest.raises(ExternalToolBatchPersistenceError):
        execute_external_tool_batch(
            agent=agent,
            assistant_message=SimpleNamespace(tool_calls=[]),
            assistant_row={"role": "assistant", "tool_calls": [{"id": "call-1"}]},
            messages=[],
            envelope=ExternalToolBatchEnvelope("task", "turn", ("call-1",)),
        )
    assert effects == []


def test_equal_intervening_rows_do_not_satisfy_exact_marker_receipt():
    durable_rows = []
    effects = []

    def flush(messages, _history=None):
        marker = messages[0]["display_metadata"]["external_tool_batch_marker"]
        durable_rows.append({"id": 1, **messages[0]})
        durable_rows.append({"id": 2, "role": "assistant",
                             "tool_calls": [{"id": "call-1"}]})
        return True

    agent = SimpleNamespace(
        session_id="durable-session",
        _session_db=SimpleNamespace(get_messages=lambda _sid: list(durable_rows)),
        _flush_messages_to_session_db=flush,
        _execute_tool_calls=lambda *_args: effects.append(True),
        _incremental_persistence_failed=False,
    )
    with pytest.raises(ExternalToolBatchPersistenceError):
        execute_external_tool_batch(
            agent=agent,
            assistant_message=SimpleNamespace(tool_calls=[]),
            assistant_row={"role": "assistant", "tool_calls": [{"id": "call-1"}]},
            messages=[],
            envelope=ExternalToolBatchEnvelope("task", "turn", ("call-1",)),
        )
    assert effects == [True]


def test_equal_call_batches_keep_distinct_marker_receipts():
    durable_rows = []

    def flush(messages, _history=None):
        for message in messages:
            marker = (message.get("display_metadata") or {}).get(
                "external_tool_batch_marker"
            )
            if marker and not any(
                (row.get("display_metadata") or {}).get("external_tool_batch_marker")
                == marker and row.get("role") == message.get("role")
                for row in durable_rows
            ):
                durable_rows.append({"id": len(durable_rows) + 1, **message})
        return True

    agent = SimpleNamespace(
        session_id="durable-session",
        _session_db=SimpleNamespace(get_messages=lambda _sid: list(durable_rows)),
        _flush_messages_to_session_db=flush,
        _incremental_persistence_failed=False,
    )

    def execute(_assistant, messages, *_args):
        messages.append({
            "role": "tool", "tool_call_id": "same-call", "content": "result",
            "display_metadata": {
                "external_tool_batch_marker": get_external_tool_batch_marker()
            },
        })
        flush(messages)

    agent._execute_tool_calls = execute
    receipts = []
    for turn in ("turn-1", "turn-2"):
        receipts.append(execute_external_tool_batch(
            agent=agent,
            assistant_message=SimpleNamespace(tool_calls=[]),
            assistant_row={"role": "assistant", "tool_calls": [{"id": "same-call"}]},
            messages=[],
            envelope=ExternalToolBatchEnvelope("task", turn, ("same-call",)),
        ))

    assert receipts[0].assistant_row_id != receipts[1].assistant_row_id
    assert receipts[0].tool_rows[0][0] != receipts[1].tool_rows[0][0]


def test_simultaneous_equal_call_batches_never_cross_stamp_markers():
    durable_rows = []
    durable_lock = threading.Lock()
    entered = threading.Barrier(2)

    def flush(messages, _history=None):
        with durable_lock:
            for message in messages:
                if not message.get("_test_persisted"):
                    durable_rows.append({"id": len(durable_rows) + 1, **message})
                    message["_test_persisted"] = True
        return True

    agent = SimpleNamespace(
        session_id="durable-session",
        _session_db=SimpleNamespace(get_messages=lambda _sid: list(durable_rows)),
        _flush_messages_to_session_db=flush,
        _incremental_persistence_failed=False,
    )

    def execute(_assistant, messages, *_args):
        entered.wait(2)
        messages.append({
            "role": "tool",
            "tool_call_id": "same-call",
            "content": messages[0]["content"],
            "display_metadata": {
                "external_tool_batch_marker": get_external_tool_batch_marker()
            },
        })
        flush(messages)

    agent._execute_tool_calls = execute
    receipts = {}
    errors = []

    def invoke(turn):
        try:
            receipts[turn] = execute_external_tool_batch(
                agent=agent,
                assistant_message=SimpleNamespace(tool_calls=[]),
                assistant_row={
                    "role": "assistant",
                    "content": turn,
                    "tool_calls": [{"id": "same-call"}],
                },
                messages=[],
                envelope=ExternalToolBatchEnvelope("task", turn, ("same-call",)),
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=invoke, args=(turn,)) for turn in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)

    assert not errors
    assert set(receipts) == {"one", "two"}
    assert receipts["one"].tool_rows[0][2] == "one"
    assert receipts["two"].tool_rows[0][2] == "two"
    assert not hasattr(agent, "_external_tool_batch_marker")
