"""Canonical host-owned external tool-batch operation contracts."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.external_tool_batch import ExternalToolBatchEnvelope, execute_external_tool_batch


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
