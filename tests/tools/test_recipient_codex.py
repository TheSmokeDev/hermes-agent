"""Exact native receipt evidence never grants existing-task control."""
import copy
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.computer_use import recipient_codex as observer

TASK = "11111111-2222-4333-8444-555555555555"
ITEM = "msg_22222222-3333-4444-8555-666666666666"
MESSAGE = "EXACT_CODEX_MESSAGE: preserve  two spaces. "


@pytest.fixture
def native_history(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    path = sessions / "task.jsonl"
    now = datetime.now(timezone.utc)
    meta = {"type": "session_meta", "payload": {"id": TASK, "originator": "Codex Desktop", "source": "vscode"}}
    record = {"type": "response_item", "timestamp": (now - timedelta(seconds=1)).isoformat(),
              "payload": {"type": "message", "role": "user", "id": ITEM,
                          "content": [{"type": "input_text", "text": MESSAGE.replace("_", "\\_") + "\n"}]}}
    with closing(sqlite3.connect(home / "state_5.sqlite")) as db:
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT)")
        db.execute("INSERT INTO threads VALUES (?,?,?)", (TASK, str(path), "vscode"))
        db.commit()
    monkeypatch.setattr(observer, "_native_context", lambda target: (home, home))
    def write(rows):
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    write([meta, record])
    return home, path, meta, record, (now - timedelta(seconds=2)).isoformat(), write


def test_unique_post_attempt_native_user_item_with_exact_visible_body(native_history):
    _, _, _, _, after, _ = native_history
    result = observer.posted_receipt({"task_id": TASK}, MESSAGE, after, [{"text": MESSAGE}])
    assert result["native_message_id"] == ITEM and result["source"] == "codex_native_history"
    assert "accepted" not in result and "completed" not in result


@pytest.mark.parametrize("fault", ["task", "originator", "source", "role", "before_attempt", "item_id",
                                  "extra_whitespace", "missing_visible", "repeated_visible", "duplicate_item",
                                  "outside_sessions", "partial_item", "process_changed"])
def test_unproven_native_item_is_not_a_receipt(native_history, monkeypatch, fault):
    home, path, meta, record, after, write = native_history
    rows, visible = [meta, record], [{"text": MESSAGE}]
    if fault == "task": meta["payload"]["id"] = "another-task"
    elif fault == "originator": meta["payload"]["originator"] = "codex_exec"
    elif fault == "source": meta["payload"]["source"] = "cli"
    elif fault == "role": record["payload"]["role"] = "assistant"
    elif fault == "before_attempt": record["timestamp"] = after
    elif fault == "item_id": record["payload"]["id"] = None
    elif fault == "extra_whitespace": record["payload"]["content"][0]["text"] += " "
    elif fault == "missing_visible": visible = []
    elif fault == "repeated_visible": visible *= 2
    elif fault == "duplicate_item":
        other = copy.deepcopy(record)
        other["payload"]["id"] = "msg_33333333-4444-4555-8666-777777777777"
        rows.append(other)
    elif fault == "outside_sessions":
        with closing(sqlite3.connect(home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET rollout_path=?", (str(home / "elsewhere.jsonl"),))
            db.commit()
        (home / "elsewhere.jsonl").write_text("", encoding="utf-8")
    elif fault == "process_changed":
        monkeypatch.setattr(observer, "_native_context", lambda target: None)
    write(rows)
    if fault == "partial_item": path.write_bytes(path.read_bytes().rstrip(b"\n"))
    assert observer.posted_receipt({"task_id": TASK}, MESSAGE, after, visible) is None


@pytest.mark.parametrize("fault", [None, "pid_reused", "exe", "owner", "unavailable", "relative_home"])
def test_native_history_home_comes_from_same_verified_process(tmp_path, monkeypatch, fault):
    from types import SimpleNamespace
    now = datetime.now(timezone.utc)
    target = {"pid": 71, "exe": str(tmp_path / "Codex.exe"), "process_started": now.isoformat(),
              "product": "Codex", "company": "OpenAI OpCo, LLC"}
    process = SimpleNamespace(exe=lambda: target["exe"] + (".other" if fault == "exe" else ""),
                              create_time=lambda: now.timestamp() + (1 if fault == "pid_reused" else 0),
                              username=lambda: "other" if fault == "owner" else "operator",
                              environ=lambda: {"USERPROFILE": str(tmp_path), **({"CODEX_HOME": "relative"} if fault == "relative_home" else {})})
    def owner(pid=None):
        if fault == "unavailable": raise observer.psutil.NoSuchProcess(71)
        return process if pid else SimpleNamespace(username=lambda: "operator")
    monkeypatch.setattr(observer.psutil, "Process", owner)
    if fault == "unavailable":
        assert observer.posted_receipt(target, MESSAGE, now.isoformat(), [{"text": MESSAGE}]) is None
    else:
        assert observer._native_context(target) == ((tmp_path / ".codex", tmp_path / ".codex") if fault is None else None)


def test_known_editor_serialization_does_not_normalize_arbitrary_text():
    assert observer._editor_content_matches(MESSAGE.replace("_", "\\_") + "\n", MESSAGE)
    assert not observer._editor_content_matches(MESSAGE.strip(), MESSAGE)
    assert not observer._editor_content_matches(MESSAGE + "\n\n", MESSAGE)
    assert not observer._editor_content_matches("literal_\n", "literal\\_")
