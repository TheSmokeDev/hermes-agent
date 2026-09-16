"""Real native files preserve identity, bounded visibility, and read-only authority."""
import json
import os
import sqlite3
import sys
from contextlib import closing
from types import SimpleNamespace

import pytest

from tools.computer_use import recipient_codex, recipient_history, recipient_history_sources as sources
from tools.computer_use.recipient_bridge import RecipientBridge
from tools.computer_use.recipient_claude import ClaudeRecipients
from tools.computer_use.recipient_contract import RecipientError
from tests.tools.test_recipient_bridge import Desktop
from tests.tools.test_recipient_claude import Native, peer as peer

TASKS = ["11111111-2222-4333-8444-555555555555", "22222222-3333-4444-8555-666666666666"]
WRITTEN = "2026-09-14T12:00:00+00:00"


def private_file(path):
    if sys.platform == "win32":
        import win32con
        import win32security
        from tools.computer_use.recipient_claude import WindowsClaudeNative
        native = WindowsClaudeNative()
        dacl = win32security.ACL()
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, native.sid)
        win32security.SetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
                                          win32security.DACL_SECURITY_INFORMATION
                                          | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                                          None, None, dacl, None)
    else:
        path.chmod(0o600)


def write_rows(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    private_file(path)


def native_row(app, task, role, text, **extra):
    if app == "codex_desktop":
        return {"type": "response_item", "timestamp": WRITTEN,
                "payload": {"type": "message", "role": role, "id": "msg:" + role + text[:10],
                            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
                            **extra}}
    return {"type": role, "sessionId": task, "timestamp": WRITTEN, "uuid": "msg:" + role + text[:10],
            "message": {"role": role, "content": [{"type": "text", "text": text}]}, **extra}


@pytest.fixture
def native_sources(tmp_path, monkeypatch):
    codex_home, claude_home = tmp_path / "codex", tmp_path / "claude"
    (codex_home / "sessions").mkdir(parents=True)
    (claude_home / "projects" / "project").mkdir(parents=True)
    paths, records = {}, {}
    with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as db:
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT, title TEXT)")
        for app in ("codex_desktop", "claude_code"):
            for task in TASKS:
                path = ((codex_home / "sessions") if app == "codex_desktop" else
                        (claude_home / "projects" / "project")) / (task + ".jsonl")
                header = ({"type": "session_meta", "payload": {"id": task, "source": "vscode",
                                                                 "originator": "Codex Desktop"}}
                          if app == "codex_desktop" else {"type": "system", "sessionId": task})
                rows = [header, native_row(app, task, "user", "same title"),
                        native_row(app, task, "assistant", "answer " + task),
                        native_row(app, task, "user", "follow up"),
                        native_row(app, task, "assistant", "final answer")]
                write_rows(path, rows)
                paths[app, task], records[app, task] = path, rows
                if app == "codex_desktop":
                    db.execute("INSERT INTO threads VALUES (?,?,?,?)", (task, str(path), "vscode", "same title"))
        db.commit()
    monkeypatch.setattr(recipient_codex, "_native_context", lambda target: (codex_home, codex_home))
    desktop, native = Desktop(), Native()
    claude = ClaudeRecipients(claude_home, native=native)
    service = RecipientBridge(session_id="session", owner="owner", state_dir=tmp_path / "state",
                              desktop=desktop, claude=claude)
    return SimpleNamespace(service=service, desktop=desktop, native=native, claude=claude,
                           paths=paths, records=records, codex_home=codex_home, claude_home=claude_home)


@pytest.mark.parametrize("app", ["codex_desktop", "claude_code"])
def test_catalog_history_pagination_is_exact_and_never_grants_control(native_sources, app):
    env, recipients = native_sources, []
    page = env.service.catalog(app=app, limit=1)
    assert page["sources"] == [{"app": app, "available": True, "reason": None}]
    while True:
        recipients.extend(page["recipients"])
        if page["next_cursor"] is None:
            break
        assert page["truncated"]
        page = env.service.catalog(app=app, limit=1, cursor=page["next_cursor"])
    assert [r["task_id"] for r in recipients] == TASKS
    assert recipients[0]["title"] == recipients[1]["title"]
    assert recipients[0]["recipient_id"] != recipients[1]["recipient_id"]
    for target in recipients:
        token = target["target_token"]
        assert target["read_only"] and target["proven_control"] == "none"
        assert "send" not in target["operations"] and "inspect" not in target["operations"]
        assert env.service.select(token)["selected"]
        status = env.service.status(target_token=token)
        assert status["status"] == "unknown" and status["available"] and not status["completion_tracking"]
        result, cursor = [], None
        while True:
            history = env.service.history(target_token=token, limit=1, cursor=cursor)
            assert all(history[k] == target[k] for k in ("recipient_id", "app", "task_id", "host_id"))
            assert history["source"]["source_id"] == target["source"]["source_id"]
            assert history["source"]["read_only"] and history["source"]["modified_at"]
            result.extend(history["messages"])
            cursor = history["next_cursor"]
            if not cursor:
                break
        assert [r["role"] for r in result] == ["user", "assistant", "user", "assistant"]
        assert [r["text"] for r in result] == ["same title", "answer " + target["task_id"], "follow up", "final answer"]
        assert all(r["timestamp"] == WRITTEN for r in result)
        with pytest.raises(RecipientError, match="recipient_control_unavailable"):
            env.service.send(target_token=token, operation_id="forbidden", message="do work")
        with pytest.raises(RecipientError, match="recipient_inspection_unavailable"):
            env.service.inspect(target_token=token, capture=True)
        assert str(env.paths[app, target["task_id"]]) not in json.dumps(history)
    assert env.desktop.composes == env.desktop.submits == env.desktop.captures == 0
    assert env.native.writes == [] and env.native.secrets_read == 0


@pytest.mark.parametrize("app", ["codex_desktop", "claude_code"])
def test_only_visible_human_and_assistant_text_survives_native_parsing(native_sources, app):
    env, task = native_sources, TASKS[0]
    rows = env.records[app, task]
    rows.extend(native_row(app, task, role, "PRIVATE_" + role) for role in ("system", "developer", "tool"))
    rows.extend(native_row(app, task, "user", "PRIVATE_" + flag, **{flag: True})
                for flag in ("hidden", "isMeta", "isSidechain", "isCompactSummary"))
    rows.append(native_row(app, task, "assistant", "PRIVATE_display", display_kind="hidden"))
    if app == "codex_desktop":
        rows.append(native_row(app, task, "assistant", "PRIVATE_reasoning", channel="analysis"))
        rows.append(native_row(app, task, "assistant", "PRIVATE_tool_call", recipient="tools.exec"))
        rows.append({"type": "event_msg", "payload": {"type": "agent_message", "message": "PRIVATE_event"}})
    else:
        tool = native_row(app, task, "user", "PRIVATE_tool_result")
        tool["message"]["content"].append({"type": "tool_result", "content": "PRIVATE_output"})
        rows.append(tool)
        thinking = native_row(app, task, "assistant", "visible final")
        thinking["message"]["content"].append({"type": "thinking", "thinking": "PRIVATE_reasoning"})
        rows.append(thinking)
    write_rows(env.paths[app, task], rows)
    token = env.service.catalog(app=app)["recipients"][0]["target_token"]
    history = env.service.history(target_token=token)
    assert "PRIVATE" not in json.dumps(history)
    assert history["messages"][1]["text"] == "answer " + task


@pytest.mark.parametrize("fault", ["limit_zero", "limit_bool", "limit_float", "limit_large", "foreign_target",
                                  "foreign_actor", "foreign_session", "foreign_profile", "expired_target",
                                  "expired_cursor", "cursor_target", "cursor_app", "changed_snapshot", "replaced_file"])
def test_tokens_and_cursors_fail_closed(native_sources, monkeypatch, tmp_path, fault):
    env = native_sources
    catalog = env.service.catalog(app="codex_desktop", limit=1)
    token = catalog["recipients"][0]["target_token"]
    history = env.service.history(target_token=token, limit=1)
    service, arguments = env.service, {"target_token": token}
    if fault.startswith("limit_"):
        arguments["limit"] = {"limit_zero": 0, "limit_bool": True, "limit_float": 1.5, "limit_large": 51}[fault]
    elif fault == "foreign_target":
        arguments["target_token"] = "unissued"
    elif fault in {"foreign_actor", "foreign_session", "foreign_profile"}:
        service = RecipientBridge(session_id="other" if fault == "foreign_session" else "session",
                                  owner="other" if fault == "foreign_actor" else "owner",
                                  state_dir=tmp_path / ("other" if fault == "foreign_profile" else "state"),
                                  desktop=env.desktop, claude=env.claude)
    elif fault in {"expired_target", "expired_cursor"}:
        now = recipient_history.time.time()
        monkeypatch.setattr(recipient_history.time, "time", lambda: now + 301)
        if fault == "expired_cursor":
            arguments["cursor"] = history["next_cursor"]
    elif fault == "cursor_target":
        arguments.update(target_token=env.service.catalog(app="codex_desktop")["recipients"][1]["target_token"],
                         cursor=history["next_cursor"])
    elif fault == "cursor_app":
        with pytest.raises(RecipientError, match="cursor_mismatch"):
            service.catalog(app="claude_code", cursor=catalog["next_cursor"])
        return
    elif fault == "changed_snapshot":
        with env.paths["codex_desktop", TASKS[0]].open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(native_row("codex_desktop", TASKS[0], "assistant", "new answer")) + "\n")
        arguments["cursor"] = history["next_cursor"]
    elif fault == "replaced_file":
        path = env.paths["codex_desktop", TASKS[0]]
        replacement = path.with_suffix(".replacement")
        write_rows(replacement, env.records["codex_desktop", TASKS[0]])
        os.replace(replacement, path)
    with pytest.raises(RecipientError):
        service.history(**arguments)
    assert env.desktop.composes == env.desktop.submits == env.desktop.captures == 0


@pytest.mark.parametrize("app", ["codex_desktop", "claude_code"])
def test_missing_and_unverifiable_native_sources_are_not_catalog_entries(native_sources, app):
    env = native_sources
    chosen = env.service.catalog(app=app)["recipients"][0]
    path = env.paths[app, TASKS[0]]
    path.unlink()
    result = env.service.status(target_token=chosen["target_token"])
    assert result["status"] == "unknown" and not result["available"]
    assert result["reason"] == "native_history_unavailable"
    rows = env.records[app, TASKS[1]]
    if app == "codex_desktop":
        rows[0]["payload"]["originator"] = "codex_exec"
    else:
        rows[1]["sessionId"] = TASKS[0]
    write_rows(env.paths[app, TASKS[1]], rows)
    assert env.service.catalog(app=app)["recipients"] == []
    if app == "codex_desktop":
        (env.codex_home / "state_5.sqlite").unlink()
    else:
        env.paths[app, TASKS[1]].unlink()
        (env.claude_home / "projects" / "project").rmdir()
        (env.claude_home / "projects").rmdir()
    source = env.service.catalog(app=app)["sources"][0]
    assert not source["available"] and source["reason"]


def test_stored_task_stays_pinned_while_live_focus_changes(native_sources):
    env = native_sources
    stored = env.service.catalog(app="codex_desktop")["recipients"][0]
    live = env.service.list_recipients(app="codex_desktop")["recipients"][0]
    assert env.service.history(target_token=live["target_token"])["messages"][1]["text"] == "answer " + TASKS[0]
    env.desktop.state["task_binding"].update(task_id=TASKS[1], deeplink="codex://threads/" + TASKS[1])
    assert env.service.history(target_token=stored["target_token"])["messages"][1]["text"] == "answer " + TASKS[0]
    with pytest.raises(RecipientError, match="recipient_task_changed"):
        env.service.history(target_token=live["target_token"])
    with pytest.raises(RecipientError, match="recipient_control_unavailable"):
        env.service.send(target_token=stored["target_token"], operation_id="no-upgrade", message="hello")


@pytest.mark.parametrize("app", ["codex_desktop", "claude_code"])
def test_history_io_and_response_bounds_preserve_truncation(native_sources, app):
    env, task = native_sources, TASKS[0]
    path = env.paths[app, task]
    filler = {"type": "system", "padding": "x" * 32000}
    rows = [env.records[app, task][0], *([filler] * 140)]
    rows += [native_row(app, task, "user", "x" * 9000) for _ in range(7)]
    # Claude must establish the exact session before the bounded tail begins.
    if app == "claude_code":
        rows.insert(1, native_row(app, task, "user", "same title"))
    write_rows(path, rows)
    with path.open("ab") as stream:
        stream.write(b'{"type":"user","message":"PRIVATE_partial')
    token = env.service.catalog(app=app)["recipients"][0]["target_token"]
    first = env.service.history(target_token=token, limit=50)
    assert first["truncated"] and first["next_cursor"]
    assert sum(len(m["text"]) for m in first["messages"]) <= recipient_history.PAGE_CHARACTERS
    assert all(len(m["text"]) <= 8000 and m["truncated"] for m in first["messages"])
    second = env.service.history(target_token=token, limit=50, cursor=first["next_cursor"])
    assert second["truncated"] and second["next_cursor"] is None
    assert len(first["messages"]) + len(second["messages"]) == 7
    raw = sources.read_file(path)
    assert len(raw["data"]) <= sources.HISTORY_BYTES and len(raw["prefix"]) <= sources.PREFIX_BYTES


@pytest.mark.parametrize("operation", ["catalog", "history", "status"])
def test_authority_revoked_during_real_read_discards_result(native_sources, monkeypatch, operation):
    env, revoked = native_sources, [False]
    token = env.service.catalog(app="codex_desktop")["recipients"][0]["target_token"]
    original = sources.read_file
    def read(*args, **kwargs):
        result = original(*args, **kwargs)
        revoked[0] = True
        return result
    def authorize():
        if revoked[0]:
            raise RecipientError("revoked", 403)
    env.service.authorize = authorize
    monkeypatch.setattr(sources, "read_file", read)
    with pytest.raises(RecipientError, match="revoked"):
        getattr(env.service, operation)(**({"app": "codex_desktop"} if operation == "catalog" else {"target_token": token}))


def test_live_claude_reads_keep_process_identity_and_ambiguous_files_unavailable(tmp_path, peer):
    claude, native, _, meta, _, path = peer
    task = meta["sessionId"]
    write_rows(path, [native_row("claude_code", task, "user", "same title"),
                      native_row("claude_code", task, "assistant", "visible response")])
    service = RecipientBridge(session_id="session", owner="owner", state_dir=tmp_path / "bridge",
                              desktop=Desktop(), claude=claude)
    token = service.list_recipients(app="claude_code")["recipients"][0]["target_token"]
    assert service.history(target_token=token)["messages"][1]["text"] == "visible response"
    copy = path.parent.parent / "other-project" / path.name
    copy.parent.mkdir()
    copy.write_bytes(path.read_bytes())
    private_file(copy)
    assert not service.status(target_token=token)["available"]
    copy.unlink()
    native.proc["process_started"] = "replacement"
    with pytest.raises(RecipientError, match="stale_claude_process"):
        service.history(target_token=token)
    assert native.writes == [] and native.secrets_read == 0


@pytest.mark.parametrize("fault", ["index_path", "forged_task", "malformed_role", "changed_focus", "candidate_cap", "record_cap"])
def test_native_source_boundaries_do_not_fall_back_to_another_task(native_sources, monkeypatch, fault):
    env, app, task = native_sources, "codex_desktop", TASKS[0]
    token = env.service.list_recipients(app=app)["recipients"][0]["target_token"]
    if fault == "index_path":
        other = env.codex_home / "outside.jsonl"
        other.write_bytes(env.paths[app, task].read_bytes())
        with closing(sqlite3.connect(env.codex_home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(other), task))
            db.commit()
        with pytest.raises(RecipientError, match="unsafe_history_source"):
            env.service.history(target_token=token)
    elif fault == "forged_task":
        env.records[app, task][0]["payload"]["id"] = TASKS[1]
        write_rows(env.paths[app, task], env.records[app, task])
        with pytest.raises(RecipientError, match="history_task_identity_unverified"):
            env.service.history(target_token=token)
    elif fault == "malformed_role":
        env.records[app, task].append(native_row(app, task, "assistant", "PRIVATE_malformed"))
        env.records[app, task][-1]["payload"]["role"] = {"unexpected": "role"}
        write_rows(env.paths[app, task], env.records[app, task])
        assert "PRIVATE" not in json.dumps(env.service.history(target_token=token))
    elif fault == "changed_focus":
        original = sources.read_file
        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            env.desktop.state["task_binding"].update(task_id=TASKS[1], deeplink="codex://threads/" + TASKS[1])
            return result
        monkeypatch.setattr(sources, "read_file", read)
        with pytest.raises(RecipientError, match="recipient_task_changed"):
            env.service.history(target_token=token)
    else:
        name = "CATALOG_CANDIDATES" if fault == "candidate_cap" else "CATALOG_RECORDS"
        monkeypatch.setattr(sources, name, 1)
        page = env.service.catalog(app=app)
        assert page["truncated"] and len(page["recipients"]) <= 1
    assert env.desktop.composes == env.desktop.submits == env.desktop.captures == 0


def junction(link, target):
    """A directory junction — the Windows reparse point an unprivileged user can make."""
    import subprocess
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True, text=True)
    return made.returncode == 0 and link.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reparse points")
def test_a_reparse_point_under_a_native_file_is_refused_on_windows(native_sources, tmp_path):
    """The POSIX reader guards the path; the Windows reader must refuse the same shape."""
    env, app, task = native_sources, "claude_code", TASKS[0]
    real = env.paths[app, task]
    token = env.service.catalog(app=app)["recipients"][0]["target_token"]
    assert env.service.history(target_token=token)["messages"][1]["text"] == "answer " + task
    # Swap the project directory for a junction pointing at an identical copy.
    moved = tmp_path / "relocated"
    moved.mkdir()
    copy = moved / real.name
    copy.write_bytes(real.read_bytes())
    private_file(copy)
    project = real.parent
    for leftover in project.iterdir():
        leftover.unlink()
    project.rmdir()
    if not junction(project, moved):
        pytest.skip("this host does not allow junction creation")
    assert copy.exists() and (project / real.name).read_bytes() == real.read_bytes()
    with pytest.raises(RecipientError, match="unsafe_history_source"):
        sources.read_file(project / real.name)
    with pytest.raises(RecipientError):
        env.service.history(target_token=token)
    assert env.desktop.composes == env.desktop.submits == env.desktop.captures == 0
    assert env.native.writes == [] and env.native.secrets_read == 0
