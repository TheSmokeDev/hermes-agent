"""The canonical host-owned operation for executing an external tool batch.

This module deliberately delegates dispatch to ``AIAgent._execute_tool_calls``.
It owns only the durable assistant/result envelope around that canonical path;
it has no registry, handler map, policy, approval, or tool dispatch logic.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import asyncio
import threading
from typing import Any, Sequence
from uuid import uuid4


_BATCH_MARKER_KEY = "external_tool_batch_marker"
_external_batch_scope: ContextVar[tuple[str, int, Any] | None] = ContextVar(
    "external_tool_batch_scope", default=None
)


def _current_task_identity() -> Any:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def get_external_tool_batch_marker() -> str | None:
    """Return the marker only to the exact invocation thread/task owner."""
    scope = _external_batch_scope.get()
    if scope is None:
        return None
    marker, thread_id, task = scope
    if thread_id != threading.get_ident() or task is not _current_task_identity():
        return None
    return marker


class ExternalToolBatchPersistenceError(RuntimeError):
    """The exact external tool batch could not be proven durable."""


@dataclass(frozen=True)
class ExternalToolBatchEnvelope:
    task_id: str
    turn_id: str
    call_ids: tuple[str, ...]
    api_call_count: int = 0
    high_water_row_id: int = 0


@dataclass(frozen=True)
class ExternalToolBatchReceipt:
    session_id: str
    turn_id: str
    assistant_row_id: int
    high_water_row_id: int
    tool_rows: tuple[tuple[int, str, Any], ...]


def _read_rows(agent: Any) -> list[dict[str, Any]]:
    db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    if db is None or not session_id:
        raise ExternalToolBatchPersistenceError("durable session is unavailable")
    rows = db.get_messages(session_id)
    if not isinstance(rows, list):
        raise ExternalToolBatchPersistenceError("durable transcript read-back failed")
    return rows


def _prove_receipt(
    agent: Any,
    envelope: ExternalToolBatchEnvelope,
    before_ids: set[int],
    marker: str,
) -> ExternalToolBatchReceipt:
    rows = _read_rows(agent)
    new_rows = [
        row for row in rows
        if type(row.get("id")) is int
        and row["id"] > envelope.high_water_row_id
        and row["id"] not in before_ids
        and (row.get("display_metadata") or {}).get(_BATCH_MARKER_KEY) == marker
    ]
    assistant_rows = [
        row for row in new_rows
        if row.get("role") == "assistant"
        and tuple(
            call.get("id") for call in (row.get("tool_calls") or [])
            if isinstance(call, dict)
        ) == envelope.call_ids
    ]
    if len(assistant_rows) != 1:
        raise ExternalToolBatchPersistenceError("exact assistant tool-call row not found")
    assistant_id = assistant_rows[0]["id"]
    tool_rows = []
    for call_id in envelope.call_ids:
        matches = [
            row for row in new_rows
            if row.get("role") == "tool"
            and row.get("tool_call_id") == call_id
            and row["id"] > assistant_id
        ]
        if len(matches) != 1:
            raise ExternalToolBatchPersistenceError(
                f"exact tool-result row not found for {call_id}"
            )
        row = matches[0]
        tool_rows.append((row["id"], call_id, row.get("content")))
    high_water = max((row["id"] for row in new_rows), default=0)
    if high_water < max(row[0] for row in tool_rows):
        raise ExternalToolBatchPersistenceError("tool-result high-water proof failed")
    return ExternalToolBatchReceipt(
        session_id=agent.session_id,
        turn_id=envelope.turn_id,
        assistant_row_id=assistant_id,
        high_water_row_id=high_water,
        tool_rows=tuple(tool_rows),
    )


def execute_external_tool_batch(
    *,
    agent: Any,
    assistant_message: Any,
    assistant_row: dict[str, Any],
    messages: list[dict[str, Any]],
    envelope: ExternalToolBatchEnvelope,
    assistant_already_appended: bool = False,
    conversation_history: Sequence[dict[str, Any]] | None = None,
    require_receipt: bool = True,
    before_execute: Any = None,
) -> ExternalToolBatchReceipt | None:
    """Persist, canonically execute once, persist results, then prove read-back."""
    if assistant_row.get("_db_persisted"):
        raise ExternalToolBatchPersistenceError("external persistence marker is forbidden")
    assistant_row.pop("_db_persisted", None)
    marker = uuid4().hex
    metadata = assistant_row.get("display_metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    metadata[_BATCH_MARKER_KEY] = marker
    assistant_row["display_metadata"] = metadata
    before_ids: set[int] = set()
    if require_receipt:
        before_ids = {
            row["id"] for row in _read_rows(agent)
            if type(row.get("id")) is int
        }
    if not assistant_already_appended:
        messages.append(assistant_row)
    elif not messages or messages[-1] is not assistant_row:
        # Conversation may append synthetic invalid-call tool rows after the
        # assistant row; identity membership still proves the exact object.
        if not any(message is assistant_row for message in messages):
            raise ValueError("assistant row is not present in messages")

    try:
        persisted = agent._flush_messages_to_session_db(
            messages, conversation_history
        )
    except Exception as exc:
        raise ExternalToolBatchPersistenceError(
            "assistant tool-call row persistence raised"
        ) from exc
    if persisted is False:
        raise ExternalToolBatchPersistenceError(
            "assistant tool-call row was not persisted before execution"
        )
    assistant_matches = [
        row for row in _read_rows(agent)
        if type(row.get("id")) is int
        and row["id"] > envelope.high_water_row_id
        and row["id"] not in before_ids
        and row.get("role") == "assistant"
        and (row.get("display_metadata") or {}).get(_BATCH_MARKER_KEY) == marker
        and tuple(
            call.get("id") for call in (row.get("tool_calls") or [])
            if isinstance(call, dict)
        ) == envelope.call_ids
    ]
    if len(assistant_matches) != 1:
        raise ExternalToolBatchPersistenceError(
            "exact marked assistant row was not durable before execution"
        )

    if before_execute is not None:
        before_execute()

    marker_token = _external_batch_scope.set(
        (marker, threading.get_ident(), _current_task_identity())
    )
    try:
        agent._execute_tool_calls(
            assistant_message,
            messages,
            envelope.task_id,
            envelope.api_call_count,
        )
    finally:
        _external_batch_scope.reset(marker_token)
    if getattr(agent, "_incremental_persistence_failed", False):
        raise ExternalToolBatchPersistenceError("tool-result persistence failed")
    if not require_receipt:
        return None
    return _prove_receipt(agent, envelope, before_ids, marker)
