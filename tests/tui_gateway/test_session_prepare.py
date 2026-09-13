"""Explicit voice preparation persists the selected native draft without a synthetic turn."""

from hermes_state import SessionDB
from tui_gateway import server


def test_prepare_persists_once_without_messages_and_rejects_wrong_identity(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda _sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
    sid = None
    try:
        result = server.handle_request({"id": "create", "method": "session.create", "params": {"source": "desktop"}})["result"]
        sid, key = result["session_id"], result["stored_session_id"]
        assert db.get_session(key) is None
        wrong = server.handle_request({"id": "wrong", "method": "session.prepare", "params": {
            "session_id": sid, "stored_session_id": "another-conversation"}})
        assert wrong["error"]["code"] == 4001
        assert db.get_session(key) is None
        for request_id in ("first", "retry"):
            prepared = server.handle_request({"id": request_id, "method": "session.prepare", "params": {
                "session_id": sid, "stored_session_id": key}})
            assert prepared["result"] == {"session_id": sid, "stored_session_id": key}
        row = db.get_session(key)
        assert row is not None
        assert not row.get("title")
        assert db.get_messages_as_conversation(key) == []
        assert not server._sessions[sid].get("running")
    finally:
        if sid:
            server._sessions.pop(sid, None)
        db.close()


def test_prepare_fails_when_no_durable_row_can_be_verified(monkeypatch):
    session = {"session_key": "stored-test"}
    monkeypatch.setattr(server, "_sess_nowait", lambda _params, _rid: (session, None))
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _session: False)
    result = server.handle_request({"id": "prepare", "method": "session.prepare", "params": {
        "session_id": "runtime-test", "stored_session_id": "stored-test"}})
    assert "error" in result
