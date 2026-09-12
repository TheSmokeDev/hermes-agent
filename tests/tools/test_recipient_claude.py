"""Existing-process peer IPC never grants authority from history or pipe writes."""
import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager

import pytest

from tools.computer_use.recipient_claude import ClaudeRecipients, WindowsClaudeNative
from tools.computer_use.recipient_contract import RecipientError


class Native:
    def __init__(self):
        self.proc = {"pid": 42, "process_started": "1234567", "exe": "C:/Claude/claude.exe",
                     "product": "Claude Code", "company": "Anthropic PBC"}
        self.server_pid = 42
        self.writes = []
        self.secrets_read = 0
        self.before_write = None

    def process(self, pid):
        assert pid == 42
        return dict(self.proc)

    def check_file(self, path):
        assert path.is_file() and not path.is_symlink()

    def read_file(self, path, limit, *, tail=False):
        self.check_file(path)
        data = path.read_bytes()
        if not tail and len(data) > limit:
            raise RecipientError("unsafe_claude_file")
        return data[-limit:] if tail else data

    def read_secret(self, path):
        self.secrets_read += 1
        return self.read_file(path, 4096)

    @contextmanager
    def connect(self, pipe):
        yield self

    def write(self, payload):
        if self.before_write:
            self.before_write(payload)
        self.writes.append(payload)


@pytest.fixture
def peer(tmp_path):
    home = tmp_path / "claude"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    native = Native()
    task = str(uuid.uuid4())
    pipe = "\\\\.\\pipe\\LOCAL\\cc-msg-" + "b" * 32
    metadata = {"pid": 42, "procStart": "1234567", "sessionId": task,
                "pidDomain": "win32:test", "peerProtocol": 1,
                "messagingSocketPath": pipe, "version": "2.1.268",
                "kind": "interactive", "name": "existing task"}
    meta_path = sessions / "42.json"
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")
    key = sessions / ("42." + hashlib.sha256(pipe.lower().encode()).hexdigest() + ".key")
    key.write_text(json.dumps({"peerToken": "a" * 32, "procStart": "1234567",
                               "pidDomain": "win32:test"}), encoding="utf-8")
    history = home / "projects" / "project" / (task + ".jsonl")
    history.parent.mkdir(parents=True)
    history.write_text("", encoding="utf-8")
    service = ClaudeRecipients(home, native=native)
    return service, native, meta_path, metadata, key, history


@pytest.mark.parametrize("fault", ["none", "stale_process", "changed_session", "wrong_server",
                                  "revoked", "bad_key", "uncertain_write"])
def test_peer_send_requires_current_owner_and_fresh_authority(peer, fault):
    service, native, meta_path, metadata, key, _ = peer
    choices = service.list_recipients()
    assert len(choices) == 1
    target = choices[0]["identity"]
    assert choices[0]["public"]["proven_control"] == "peer_ipc"
    assert "inspect" not in choices[0]["public"]["operations"]
    assert service.select(target)["task_id"] == metadata["sessionId"]
    assert native.secrets_read == 0
    if fault == "stale_process":
        native.proc["process_started"] = "replacement"
    elif fault == "changed_session":
        metadata["sessionId"] = str(uuid.uuid4())
        meta_path.write_text(json.dumps(metadata), encoding="utf-8")
    elif fault == "wrong_server":
        native.server_pid = 99
    elif fault == "bad_key":
        key.write_text(json.dumps({"childToken": "a" * 32}), encoding="utf-8")
    elif fault == "uncertain_write":
        def fail(payload):
            native.writes.append(payload)
            raise OSError("pipe closed")
        native.before_write = fail
    checks = []
    def authorize():
        checks.append(True)
        if fault == "revoked" and len(checks) == 2:
            raise RecipientError("binding_revoked", 403)
    if fault == "revoked":
        with pytest.raises(RecipientError, match="binding_revoked"):
            service.send(target, "keep the original job", "scoped-op", authorize=authorize)
    else:
        receipt = service.send(target, "keep the original job", "scoped-op", authorize=authorize)
        assert receipt["status"] == ("unknown" if fault in {"none", "uncertain_write"} else "failed")
    assert len(native.writes) == (1 if fault in {"none", "uncertain_write"} else 0)
    if native.writes:
        auth, message = map(json.loads, native.writes[0].splitlines())
        assert set(auth) == {"type", "token"} and auth["type"] == "auth"
        assert message["uuid"] == message["msg_id"]
        assert message["session_id"] == target["task_id"]
        assert message["msgV"] == 1 and message["priority"] == "next"
        assert message["message"] == {"role": "user", "content": "keep the original job"}
        assert "from" not in message and "childToken" not in auth
    assert service.reconcile(target, "scoped-op", message="keep the original job")["status"] != "posted"
    assert len(native.writes) == (1 if fault in {"none", "uncertain_write"} else 0)


@pytest.mark.parametrize("fault", ["none", "wrong_uuid", "wrong_session", "wrong_origin",
                                  "wrong_message", "missing_message", "partial_row"])
def test_peer_reconcile_requires_exact_posted_native_message(peer, fault):
    service, native, _, _, _, history = peer
    target = service.list_recipients()[0]["identity"]
    service.send(target, "original", "scoped-op", authorize=lambda: None)
    wire = json.loads(native.writes[0].splitlines()[1])
    row = {"type": "user", "sessionId": target["task_id"], "uuid": wire["uuid"],
           "origin": {"kind": "peer", "msg_id": wire["msg_id"]},
           "message": {"role": "user", "content": "original"}}
    if fault == "wrong_uuid": row["uuid"] = str(uuid.uuid4())
    if fault == "wrong_session": row["sessionId"] = str(uuid.uuid4())
    if fault == "wrong_origin": row["origin"]["kind"] = "human"
    if fault == "wrong_message": row["message"]["content"] = "something else"
    history.write_text(json.dumps(row) + ("" if fault == "partial_row" else "\n"), encoding="utf-8")
    receipt = service.reconcile(target, "scoped-op", message=None if fault == "missing_message" else "original")
    assert receipt["status"] == ("posted" if fault == "none" else "unknown")
    if fault == "none":
        assert receipt["message_receipt"]["native_message_id"] == wire["msg_id"]
        assert service.reconcile(target, "scoped-op", message="original") == receipt
    assert len(native.writes) == 1 and native.secrets_read == 1


@pytest.mark.windows_only
@pytest.mark.parametrize("wrong_pid", [False, True])
def test_windows_pipe_checks_real_server_before_writing(tmp_path, wrong_pid):
    import win32file
    import win32pipe

    native = WindowsClaudeNative()
    owner = native.process(os.getpid())
    assert owner["pid"] == os.getpid() and int(owner["process_started"]) > 0
    path = "\\\\.\\pipe\\LOCAL\\cc-msg-" + uuid.uuid4().hex
    server = win32pipe.CreateNamedPipe(path, win32pipe.PIPE_ACCESS_INBOUND,
                                      win32pipe.PIPE_TYPE_BYTE, 1, 65536, 65536, 5000, None)
    received, errors = [], []
    def read():
        try:
            win32pipe.ConnectNamedPipe(server, None)
            try:
                received.append(win32file.ReadFile(server, 65536)[1])
            except Exception as exc:
                if getattr(exc, "winerror", None) != 109:
                    raise
        except Exception as exc:
            errors.append(type(exc).__name__)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        with native.connect(path) as connection:
            expected = os.getpid() + 1 if wrong_pid else os.getpid()
            if connection.server_pid == expected:
                connection.write(b"owned test payload\n")
        reader.join(5)
        assert not reader.is_alive() and not errors
        assert received == ([] if wrong_pid else [b"owned test payload\n"])
    finally:
        server.Close()


@pytest.mark.windows_only
@pytest.mark.parametrize("acl", ["private", "everyone", "null"])
def test_windows_credential_handle_enforces_acl_and_native_failures(tmp_path, acl):
    import win32con
    import win32security

    native = WindowsClaudeNative()
    key = tmp_path / "peer.key"
    key.write_text('{"peerToken":"' + "a" * 32 + '"}', encoding="utf-8")
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, native.sid)
    if acl == "everyone":
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_READ,
                                win32security.ConvertStringSidToSid("S-1-1-0"))
    win32security.SetNamedSecurityInfo(str(key), win32security.SE_FILE_OBJECT,
                                      win32security.DACL_SECURITY_INFORMATION
                                      | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                                      None, None, None if acl == "null" else dacl, None)
    if acl == "private":
        native.check_file(key)
        assert json.loads(native.read_secret(key))["peerToken"] == "a" * 32
    else:
        with pytest.raises(RecipientError, match="unsafe_claude_file_acl"):
            native.check_file(key)
    with pytest.raises(RecipientError, match="claude_process_unavailable"):
        native.process(0)
    with pytest.raises(OSError, match="claude_pipe_unavailable"):
        with native.connect("\\\\.\\pipe\\LOCAL\\cc-msg-" + uuid.uuid4().hex):
            pytest.fail("missing pipe must not connect")
