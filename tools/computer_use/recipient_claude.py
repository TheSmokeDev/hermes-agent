"""Authenticated next-turn delivery to an existing native Windows Claude process.

The recipient bridge owns the durable operation journal. A pipe write is never
an acknowledgement: only the exact peer-origin session row proves posting.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid
from contextlib import contextmanager

from tools.computer_use.recipient_contract import RecipientError, digest, identifier

_PIPE = re.compile(r"\\\\\.\\pipe\\(?:local\\)?cc-msg-[0-9a-f]{32}", re.I)
_TOKEN = re.compile(r"[0-9a-f]{32}")
_ID_FIELDS = ("pid", "process_started", "exe", "task_id", "pipe", "pid_domain", "peer_protocol")
_MESSAGE_NAMESPACE = uuid.UUID("f44ba2ea-9ae1-4ca0-8d96-07c54f6310ad")


def _message_id(operation_id):
    return str(uuid.uuid5(_MESSAGE_NAMESPACE, identifier(operation_id, "operation_id")))


def _json(data):
    try:
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise RecipientError("invalid_claude_metadata") from None


@contextmanager
def _handle(handle):
    try:
        yield handle
    finally:
        handle.Close()


class ClaudeRecipients:
    def __init__(self, claude_home=None, *, native=None):
        self.home = Path(claude_home or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        self.native = native if native is not None else WindowsClaudeNative()

    def _load(self, pid):
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise RecipientError("invalid_claude_pid")
        meta = _json(self.native.read_file(self.home / "sessions" / f"{pid}.json", 65536))
        pipe = meta.get("messagingSocketPath", "")
        if (meta.get("pid") != pid or meta.get("peerProtocol") != 1
                or not isinstance(pipe, str) or not _PIPE.fullmatch(pipe)
                or meta.get("kind") not in {"interactive", "background"}):
            raise RecipientError("unsupported_claude_peer")
        try:
            task = str(uuid.UUID(meta["sessionId"]))
        except (KeyError, ValueError, TypeError, AttributeError):
            raise RecipientError("invalid_claude_session") from None
        process = self.native.process(pid)
        started = str(meta.get("procStart") or meta.get("procStartFt") or "")
        if process.get("process_started") != started:
            raise RecipientError("stale_claude_process")
        exe = str(process.get("exe", ""))
        if (process.get("product") != "Claude Code"
                or process.get("company") not in {"Anthropic PBC", "Anthropic, PBC", "Anthropic"}):
            raise RecipientError("unverified_claude_executable")
        domain = meta.get("pidDomain")
        if not isinstance(domain, str) or not domain.startswith("win32:"):
            raise RecipientError("unsupported_claude_pid_domain")
        identity = {"pid": pid, "process_started": started, "exe": exe,
                    "task_id": task, "pipe": pipe.lower(), "pid_domain": domain,
                    "peer_protocol": 1}
        self.native.check_file(self._key_path(identity))
        public = {"recipient_id": "claude-peer:" + digest(identity)[:32], "app": "claude_code",
                  "task_id": task, "title": str(meta.get("name") or f"Claude {pid}")[:256],
                  "proven_control": "peer_ipc", "live_owner": True,
                  "operations": ["select", "send", "reconcile"]}
        return {"identity": identity, "public": public}

    def _key_path(self, identity):
        hashed = hashlib.sha256(identity["pipe"].encode()).hexdigest()
        return self.home / "sessions" / f"{identity['pid']}.{hashed}.key"

    def list_recipients(self):
        records = []
        for path in sorted((self.home / "sessions").glob("*.json")):
            if not path.stem.isdecimal():
                continue
            try:
                records.append(self._load(int(path.stem)))
            except (RecipientError, OSError):
                continue
        return records

    def select(self, identity):
        current = self._load(identity.get("pid"))
        if any(current["identity"][key] != identity.get(key) for key in _ID_FIELDS):
            raise RecipientError("stale_claude_recipient")
        return current["public"]

    def _token(self, identity):
        key = _json(self.native.read_secret(self._key_path(identity)))
        token = key.get("peerToken")
        if (not isinstance(token, str) or not _TOKEN.fullmatch(token)
                or str(key.get("procStart") or key.get("procStartFt") or "") != identity["process_started"]
                or key.get("pidDomain") != identity["pid_domain"]):
            raise RecipientError("invalid_claude_peer_credential")
        return token

    def send(self, identity, message, operation_id, *, authorize):
        native_id = _message_id(operation_id)
        if not isinstance(message, str) or not message.strip() or len(message.encode("utf-8")) > 65536:
            raise RecipientError("invalid_message", 400)
        # The facade persists attempted/unknown before entering this method. No
        # reconnect or retry occurs here, including after a partial WriteFile.
        attempted = False
        try:
            self.select(identity)
            authorize()
            with self.native.connect(identity["pipe"]) as pipe:
                if pipe.server_pid != identity["pid"]:
                    raise RecipientError("wrong_claude_pipe_owner")
                self.select(identity)
                token = self._token(identity)
                payload = {"msgV": 1, "msg_id": native_id, "uuid": native_id,
                           "session_id": identity["task_id"], "type": "user",
                           "message": {"role": "user", "content": message}, "priority": "next"}
                wire = (json.dumps({"type": "auth", "token": token}) + "\n"
                        + json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                self.select(identity)
                authorize()
                attempted = True
                pipe.write(wire)
        except RecipientError as exc:
            # Authorization loss is returned to the caller, never disguised as
            # an ordinary unavailable recipient or successful delivery.
            if exc.status in {401, 403}:
                raise
            return {"status": "unknown" if attempted else "failed", "reason": exc.code}
        except OSError:
            return {"status": "unknown" if attempted else "failed", "reason": "claude_pipe_unavailable"}
        return self.reconcile(identity, operation_id, message=message)

    def reconcile(self, identity, operation_id, *, message=None):
        native_id = _message_id(operation_id)
        if not isinstance(message, str):
            return {"status": "unknown", "reason": "original_message_required"}
        try:
            self.select(identity)
            matches = list((self.home / "projects").glob(f"*/{identity['task_id']}.jsonl"))
            if len(matches) != 1:
                return {"status": "unknown", "reason": "claude_receipt_unavailable"}
            # A bounded tail can yield unknown for an old receipt; it can never
            # manufacture delivery or authorize another write of the same ID.
            rows = self.native.read_file(matches[0], 4 * 1024 * 1024, tail=True).splitlines(keepends=True)
            for raw in rows:
                if not raw.endswith(b"\n"):
                    continue
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(row, dict):
                    continue
                origin, body = row.get("origin"), row.get("message")
                if (row.get("type") != "user" or row.get("uuid") != native_id
                        or row.get("sessionId") != identity["task_id"]
                        or not isinstance(origin, dict) or origin.get("kind") != "peer"
                        or origin.get("msg_id") != native_id or not isinstance(body, dict)
                        or body.get("role") != "user"):
                    continue
                content = body.get("content")
                if isinstance(content, list) and all(isinstance(p, dict) and p.get("type") == "text"
                                                     and isinstance(p.get("text"), str) for p in content):
                    content = "".join(p.get("text", "") for p in content)
                if content == message:
                    self.select(identity)
                    return {"status": "posted", "message_receipt": {
                        "native_message_id": native_id, "session_id": identity["task_id"],
                        "content_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
                        "source": "claude_session_history"}}
        except (RecipientError, OSError):
            pass
        return {"status": "unknown", "reason": "claude_posting_not_observed"}


class WindowsClaudeNative:
    """OS checks stay at the same handle used for credential reads and writes."""

    def __init__(self):
        self.available = sys.platform == "win32"
        if not self.available:
            return
        import win32api
        import win32con
        import win32security
        self.api, self.con, self.security = win32api, win32con, win32security
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        from ctypes import wintypes
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.kernel.GetProcessTimes.restype = wintypes.BOOL
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                          wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        self.kernel.CancelIoEx.restype = wintypes.BOOL
        with _handle(win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)) as token:
            self.sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]

    def process(self, pid):
        if not self.available:
            raise RecipientError("claude_native_windows_required")
        from ctypes import wintypes
        try:
            with _handle(self.api.OpenProcess(0x1000, False, pid)) as handle:
                times = [wintypes.FILETIME() for _ in range(4)]
                if not self.kernel.GetProcessTimes(int(handle), *[ctypes.byref(t) for t in times]):
                    raise OSError
                length, image = wintypes.DWORD(32768), ctypes.create_unicode_buffer(32768)
                if not self.kernel.QueryFullProcessImageNameW(int(handle), 0, image, ctypes.byref(length)):
                    raise OSError
                with _handle(self.security.OpenProcessToken(handle, self.con.TOKEN_QUERY)) as token:
                    owner = self.security.GetTokenInformation(token, self.security.TokenUser)[0]
                if owner != self.sid:
                    raise RecipientError("wrong_claude_process_owner")
                started = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
                product, company = None, None
                try:
                    translations = self.api.GetFileVersionInfo(image.value, "\\VarFileInfo\\Translation")
                    for language, page in translations:
                        base = f"\\StringFileInfo\\{language:04x}{page:04x}\\"
                        product = self.api.GetFileVersionInfo(image.value, base + "ProductName")
                        company = self.api.GetFileVersionInfo(image.value, base + "CompanyName")
                        if product and company:
                            break
                except self.api.error:
                    pass
                return {"pid": pid, "process_started": str(started), "exe": image.value,
                        "product": product, "company": company}
        except (OSError, self.api.error):
            raise RecipientError("claude_process_unavailable") from None

    @contextmanager
    def _file(self, path, *, read=False, changing=False):
        if not self.available:
            raise RecipientError("claude_native_windows_required")
        import win32file
        path = Path(path).absolute()
        # Refuse junctions as well as symlinks, including every ancestor. The
        # opened handle is then checked again, so replacement cannot redirect a
        # key read to a different file between the path and ACL checks.
        for parent in (path, *path.parents):
            if parent.lstat().st_file_attributes & 0x400:
                raise RecipientError("unsafe_claude_file")
        access = self.con.READ_CONTROL | (self.con.GENERIC_READ if read else 0)
        share = self.con.FILE_SHARE_READ | (self.con.FILE_SHARE_WRITE if changing else 0)
        with _handle(win32file.CreateFile(str(path), access, share, None, self.con.OPEN_EXISTING,
                                          0x200000, None)) as handle:
            info = win32file.GetFileInformationByHandle(handle)
            if info[0] & (0x400 | self.con.FILE_ATTRIBUTE_DIRECTORY):
                raise RecipientError("unsafe_claude_file")
            actual = win32file.GetFinalPathNameByHandle(handle, 0)
            if actual.startswith("\\\\?\\"):
                actual = actual[4:]
            if os.path.normcase(actual) != os.path.normcase(str(path)):
                raise RecipientError("unsafe_claude_file")
            sd = self.security.GetSecurityInfo(handle, self.security.SE_FILE_OBJECT,
                                               self.security.OWNER_SECURITY_INFORMATION
                                               | self.security.DACL_SECURITY_INFORMATION)
            dacl = sd.GetSecurityDescriptorDacl()
            if sd.GetSecurityDescriptorOwner() != self.sid or dacl is None:
                raise RecipientError("unsafe_claude_file_acl")
            broad = {"S-1-1-0", "S-1-5-7", "S-1-5-11", "S-1-5-32-545", "S-1-5-32-546"}
            for i in range(dacl.GetAceCount()):
                ace = dacl.GetAce(i)
                if ace[0][1] & self.con.INHERIT_ONLY_ACE:
                    continue
                if ace[0][0] in {self.security.ACCESS_ALLOWED_ACE_TYPE,
                                  self.security.ACCESS_ALLOWED_OBJECT_ACE_TYPE}:
                    if self.security.ConvertSidToStringSid(ace[-1]) in broad and ace[1] & 0xC00D019F:
                        raise RecipientError("unsafe_claude_file_acl")
            yield handle

    def check_file(self, path):
        if not self.available:
            raise RecipientError("claude_native_windows_required")
        try:
            with self._file(path):
                pass
        except self.api.error:
            raise RecipientError("claude_file_unavailable") from None

    def read_file(self, path, limit, *, tail=False):
        if not self.available:
            raise RecipientError("claude_native_windows_required")
        import win32file
        try:
            with self._file(path, read=True, changing=tail) as handle:
                size = win32file.GetFileSize(handle)
                if not tail and size > limit:
                    raise RecipientError("oversized_claude_file")
                if tail and size > limit:
                    win32file.SetFilePointer(handle, size - limit, self.con.FILE_BEGIN)
                if size == 0:
                    return b""
                return win32file.ReadFile(handle, min(size, limit))[1]
        except self.api.error:
            raise RecipientError("claude_file_unavailable") from None

    def read_secret(self, path):
        return self.read_file(path, 4096)

    @contextmanager
    def connect(self, pipe):
        if not self.available or not isinstance(pipe, str) or not _PIPE.fullmatch(pipe):
            raise RecipientError("unsupported_claude_pipe")
        import win32file
        import win32pipe
        # Identification SQOS prevents a named-pipe server impersonating this
        # process. No retry: busy/missing/replaced owners require reconciliation.
        try:
            with _handle(win32file.CreateFile(pipe, self.con.GENERIC_WRITE, 0, None, self.con.OPEN_EXISTING,
                                              self.con.FILE_FLAG_OVERLAPPED | 0x100000 | 0x10000, None)) as handle:
                yield _WindowsPipe(handle, win32pipe.GetNamedPipeServerProcessId(handle), self.kernel)
        except self.api.error:
            raise OSError("claude_pipe_unavailable") from None


class _WindowsPipe:
    def __init__(self, handle, server_pid, kernel):
        self.handle, self.server_pid, self.kernel = handle, server_pid, kernel

    def write(self, payload):
        import pywintypes
        import win32event
        import win32file
        operation = pywintypes.OVERLAPPED()
        operation.hEvent = win32event.CreateEvent(None, True, False, None)
        try:
            code, _ = win32file.WriteFile(self.handle, payload, operation)
            if code not in {0, 997}:
                raise OSError("claude_pipe_write_failed")
            if win32event.WaitForSingleObject(operation.hEvent, 5000) != win32event.WAIT_OBJECT_0:
                self.kernel.CancelIoEx(int(self.handle), None)
                # Drain cancellation before releasing the OVERLAPPED buffer.
                try:
                    win32file.GetOverlappedResult(self.handle, operation, True)
                except (OSError, pywintypes.error):
                    pass
                raise OSError("claude_pipe_write_timeout")
            if win32file.GetOverlappedResult(self.handle, operation, False) != len(payload):
                raise OSError("claude_pipe_partial_write")
        except pywintypes.error:
            raise OSError("claude_pipe_write_failed") from None
        finally:
            operation.hEvent.Close()
