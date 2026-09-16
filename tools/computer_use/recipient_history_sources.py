"""Bounded native transcripts, independent of permission to control an application."""
from __future__ import annotations

import json
import hashlib
import os
import socket
import sqlite3
import stat
import sys
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import psutil

from tools.computer_use import recipient_codex
from tools.computer_use.recipient_contract import RecipientError, app_identity, digest, window_identity

PREFIX_BYTES = 64 * 1024
HISTORY_BYTES = 4 * 1024 * 1024
CATALOG_CANDIDATES = 1000
CATALOG_RECORDS = 200
HOST_ID = "host:" + digest(socket.gethostname())[:32]


def native_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
    except ValueError:
        return None


def _safe_path(path):
    for parent in (path, *path.parents):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RecipientError("unsafe_history_source")


if sys.platform == "win32":
    def _read_native(path, prefix_only):
        from tools.computer_use.recipient_claude import WindowsClaudeNative
        import win32file

        native = WindowsClaudeNative()
        _safe_path(path)
        try:
            with native._file(path, read=True, changing=True) as handle:
                before = win32file.GetFileInformationByHandle(handle)
                size = win32file.GetFileSize(handle)
                prefix = win32file.ReadFile(handle, min(size, PREFIX_BYTES))[1] if size else b""
                offset = max(0, size - HISTORY_BYTES) if not prefix_only else 0
                data = prefix
                if not prefix_only:
                    win32file.SetFilePointer(handle, offset, 0)
                    data = win32file.ReadFile(handle, min(size, HISTORY_BYTES))[1] if size else b""
                after = win32file.GetFileInformationByHandle(handle)
                # Reading can advance last-access time without changing the snapshot.
                if any(before[i] != after[i] for i in (0, 1, 3, 4, 5, 6, 8, 9)):
                    raise RecipientError("history_source_changed")
        except native.api.error as exc:
            raise RecipientError("native_history_unavailable", 503) from exc
        _safe_path(path)
        identity = [before[4], before[8], before[9]]
        modified = before[3].timestamp()
        return prefix, data, offset, size, identity, modified
else:
    def _read_native(path, prefix_only):
        _safe_path(path)
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():  # windows-footgun: ok — POSIX branch only
                raise RecipientError("unsafe_history_source")
            prefix = stream.read(PREFIX_BYTES)
            offset = max(0, before.st_size - HISTORY_BYTES) if not prefix_only else 0
            data = prefix
            if not prefix_only:
                stream.seek(offset)
                data = stream.read(HISTORY_BYTES)
            after = os.fstat(stream.fileno())
            _safe_path(path)
            current = path.stat()
            signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            if signature(before) != signature(after) or signature(before) != signature(current):
                raise RecipientError("history_source_changed")
        return prefix, data, offset, before.st_size, [before.st_dev, before.st_ino], before.st_mtime


def read_file(path, *, prefix_only=False):
    try:
        prefix, data, offset, size, file_id, modified = _read_native(path, prefix_only)
    except RecipientError:
        raise
    except (OSError, ImportError) as exc:
        raise RecipientError("native_history_unavailable", 503) from exc
    return {"prefix": prefix, "data": data, "offset": offset, "size": size, "file_id": file_id,
            "modified_at": datetime.fromtimestamp(modified, timezone.utc).isoformat(),
            "fingerprint": digest([file_id, size, modified, hashlib.sha256(data).hexdigest()])}


def rows(data, *, offset=0):
    lines = data.splitlines(keepends=True)
    truncated = bool(offset)
    if offset and lines:
        lines.pop(0)
    result = []
    for line in lines:
        if not line.endswith(b"\n") or len(line) > PREFIX_BYTES:
            truncated = True
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            truncated = True
            continue
        if isinstance(row, dict):
            result.append(row)
    return result, truncated


def _uuid(value):
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _visible(row):
    return (not any(row.get(key) for key in ("hidden", "isMeta", "isSidechain", "isCompactSummary"))
            and row.get("display_kind") != "hidden"
            and row.get("visibility") in (None, "visible", "public")
            and row.get("isVisibleInTranscript") is not False)


def _verify_prefix(app, prefix, task_id):
    records, _ = rows(prefix)
    if app == "codex_desktop":
        if b"\n" not in prefix:
            raise RecipientError("history_task_identity_unverified")
        try:
            first = json.loads(prefix.split(b"\n", 1)[0])
        except (ValueError, UnicodeError):
            raise RecipientError("history_task_identity_unverified") from None
        meta = first.get("payload") if isinstance(first, dict) else None
        if (not isinstance(meta, dict) or first.get("type") != "session_meta"
                or meta.get("id") != task_id or meta.get("originator") != "Codex Desktop"
                or meta.get("source") != "vscode"):
            raise RecipientError("history_task_identity_unverified")
    else:
        ids = {row["sessionId"] for row in records if isinstance(row.get("sessionId"), str)}
        if ids != {task_id}:
            raise RecipientError("history_task_identity_unverified")


def messages(app, data, task_id, offset):
    records, truncated = rows(data, offset=offset)
    result = []
    for index, row in enumerate(records):
        if not _visible(row):
            continue
        if app == "codex_desktop":
            body = row.get("payload")
            if (row.get("type") != "response_item" or not isinstance(body, dict)
                    or body.get("type") != "message" or body.get("role") not in ("user", "assistant")
                    or body.get("channel") not in (None, "final", "commentary")
                    or body.get("recipient") not in (None, "all")):
                continue
            role = body["role"]
            text_type = "input_text" if role == "user" else "output_text"
            native_id = body.get("id")
        else:
            if row.get("sessionId") not in (None, task_id):
                raise RecipientError("history_task_identity_unverified")
            body = row.get("message")
            if (row.get("sessionId") != task_id or row.get("type") not in ("user", "assistant")
                    or not isinstance(body, dict) or body.get("role") != row["type"]):
                continue
            role, text_type, native_id = body["role"], "text", row.get("uuid")
        if not _visible(body):
            continue
        content = body.get("content")
        if isinstance(content, str) and app == "claude_code":
            text = content
        elif isinstance(content, list):
            # Mixed tool-result user records are model context, not human messages.
            if role == "user" and any(not isinstance(p, dict) or p.get("type") not in (
                    text_type, "image", "input_image") for p in content):
                continue
            text = "".join(p["text"] for p in content if isinstance(p, dict)
                           and p.get("type") == text_type and isinstance(p.get("text"), str) and _visible(p))
        else:
            continue
        if not text:
            continue
        message_id = native_id if isinstance(native_id, str) and len(native_id) <= 256 else "row:" + str(index)
        result.append({"id": message_id, "role": role, "text": text[:8000],
                       "timestamp": native_time(row.get("timestamp")), "truncated": len(text) > 8000})
        truncated |= len(text) > 8000
    return result, truncated


class NativeHistory:
    def __init__(self, desktop, claude, authorize):
        self.desktop, self.claude, self.authorize = desktop, claude, authorize

    def _context(self, anchor):
        try:
            context = recipient_codex._native_context(anchor)
        except (OSError, ValueError, KeyError, psutil.Error) as exc:
            raise RecipientError("codex_history_unavailable", 503) from exc
        if context is None:
            raise RecipientError("codex_history_unavailable", 503)
        return context

    def _index(self, context, task_id=None):
        _, sqlite_home = context
        path = sqlite_home / "state_5.sqlite"
        try:
            _safe_path(path)
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                db.set_progress_handler(lambda: 1, 1000000)
                columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
                title = "title" if "title" in columns else "id"
                query = f"SELECT id, rollout_path, source, {title} FROM threads"
                if task_id is not None:
                    return db.execute(query + " WHERE id=?", (task_id,)).fetchall()
                return db.execute(query + " ORDER BY id LIMIT ?", (CATALOG_CANDIDATES + 1,)).fetchall()
        except (OSError, sqlite3.Error) as exc:
            raise RecipientError("codex_history_index_unavailable", 503) from exc

    def _record(self, app, path, task_id, title, *, anchor=None, context=None):
        self.authorize()
        if not _uuid(task_id):
            raise RecipientError("history_task_identity_unverified")
        data = read_file(path, prefix_only=True)
        _verify_prefix(app, data["prefix"], task_id)
        if app == "codex_desktop":
            kind = "codex_native_history"
        else:
            kind = "claude_session_history"
            visible, _ = messages(app, data["prefix"], task_id, 0)
            title = next((m["text"] for m in visible if m["role"] == "user"), title)
        source_id = "source:" + digest([HOST_ID, app, str(path), data["file_id"]])[:32]
        public = {"recipient_id": "history:" + digest([source_id, task_id])[:32], "app": app,
                  "task_id": task_id, "title": str(title or task_id)[:256], "host_id": HOST_ID,
                  "proven_control": "none", "read_only": True, "operations": ["select", "history", "status"],
                  "source": {"kind": kind, "source_id": source_id, "modified_at": data["modified_at"],
                             "read_only": True}}
        identity = {"app": app, "task_id": task_id, "path": str(path), "file_id": data["file_id"]}
        if anchor is not None and context is not None:
            identity.update(anchor=anchor, context=[str(p) for p in context])
        return {"backend": "native_history", "identity": identity, "public": public}

    def _codex_path(self, context, row):
        home, _ = context
        path = Path(row[1])
        if not path.is_absolute() or row[2] != "vscode" or path.suffix != ".jsonl":
            raise RecipientError("history_task_identity_unverified")
        _safe_path(path)
        if not path.resolve().is_relative_to((home / "sessions").resolve()):
            raise RecipientError("unsafe_history_source")
        return path

    def _codex_catalog(self):
        contexts, records, examined, truncated = set(), [], 0, False
        available = False
        for window in self.desktop.windows()[:CATALOG_CANDIDATES]:
            if app_identity(window) != "codex_desktop":
                continue
            anchor = window_identity(window)
            context = self._context(anchor)
            if context in contexts:
                continue
            contexts.add(context)
            index = self._index(context)
            available = True
            for row in index:
                examined += 1
                if examined > CATALOG_CANDIDATES or len(records) >= CATALOG_RECORDS:
                    return records, available, True
                try:
                    path = self._codex_path(context, row)
                    records.append(self._record("codex_desktop", path, row[0], row[3],
                                                anchor=anchor, context=context))
                except (RecipientError, OSError) as exc:
                    if isinstance(exc, RecipientError) and exc.status in {401, 403}:
                        raise
            if self._context(anchor) != context:
                raise RecipientError("history_source_changed")
        return records, available, truncated

    def _claude_paths(self):
        root = self.claude().home / "projects"
        if not root.is_dir():
            return [], False, False
        _safe_path(root)
        paths, examined = [], 0
        with os.scandir(root) as projects:
            for project in projects:
                examined += 1
                if examined > CATALOG_CANDIDATES:
                    return paths, True, True
                if not project.is_dir(follow_symlinks=False):
                    continue
                _safe_path(Path(project.path))
                with os.scandir(project.path) as files:
                    for file in files:
                        examined += 1
                        if examined > CATALOG_CANDIDATES:
                            return paths, True, True
                        path = Path(file.path)
                        if path.suffix != ".jsonl" or not _uuid(path.stem):
                            continue
                        paths.append(path)
        return paths, True, False

    def _claude_catalog(self):
        paths, available, truncated = self._claude_paths()
        records = []
        for path in paths:
            if len(records) >= CATALOG_RECORDS:
                return records, available, True
            try:
                records.append(self._record("claude_code", path, path.stem, path.stem))
            except (RecipientError, OSError) as exc:
                if isinstance(exc, RecipientError) and exc.status in {401, 403}:
                    raise
        return records, available, truncated

    def catalog(self, app):
        records, sources, truncated = [], [], False
        readers = {"codex_desktop": self._codex_catalog, "claude_code": self._claude_catalog}
        for name, reader in readers.items():
            if app and name != app:
                continue
            self.authorize()
            reason = None
            try:
                found, available, omitted = reader()
            except (RecipientError, OSError) as exc:
                if isinstance(exc, RecipientError) and exc.status in {401, 403}:
                    raise
                found, available, omitted = [], False, False
                reason = exc.code if isinstance(exc, RecipientError) else "native_history_unavailable"
            records.extend(found)
            truncated |= omitted
            sources.append({"app": name, "available": available,
                            "reason": reason or (None if available else "native_history_unavailable")})
        self.authorize()
        unique = {r["public"]["recipient_id"]: r for r in records}
        records = sorted(unique.values(), key=lambda r: (r["public"]["app"], r["public"]["task_id"],
                                                         r["public"]["recipient_id"]))
        return records[:CATALOG_RECORDS], sources, truncated or len(records) > CATALOG_RECORDS

    def resolve(self, target):
        if target.get("backend") == "native_history":
            return target
        identity, app = target["identity"], target["public"]["app"]
        if app == "codex_desktop":
            context = self._context(identity)
            found = self._index(context, identity["task_id"])
            if len(found) != 1:
                raise RecipientError("native_history_unavailable", 503)
            path = self._codex_path(context, found[0])
            return self._record(app, path, identity["task_id"], target["public"]["title"],
                                anchor=identity, context=context)
        paths, _, truncated = self._claude_paths()
        found = [path for path in paths if path.stem == identity["task_id"]]
        if len(found) != 1 or truncated:
            raise RecipientError("native_history_unavailable", 503)
        return self._record(app, found[0], identity["task_id"], target["public"]["title"])

    def read(self, record, *, prefix_only=False):
        self.authorize()
        identity = record["identity"]
        app, task_id, path = identity["app"], identity["task_id"], Path(identity["path"])
        if app == "codex_desktop":
            context = self._context(identity["anchor"])
            found = self._index(context, task_id)
            if ([str(p) for p in context] != identity["context"] or len(found) != 1
                    or self._codex_path(context, found[0]) != path):
                raise RecipientError("history_source_changed")
        else:
            root = self.claude().home / "projects"
            if path.parent.parent != root or path.name != task_id + ".jsonl":
                raise RecipientError("history_source_changed")
        data = read_file(path, prefix_only=prefix_only)
        if data["file_id"] != identity["file_id"]:
            raise RecipientError("history_source_changed")
        _verify_prefix(app, data["prefix"], task_id)
        if app == "codex_desktop" and (self._context(identity["anchor"]) != context
                                       or self._index(context, task_id) != found):
            raise RecipientError("history_source_changed")
        self.authorize()
        source = {**record["public"]["source"], "modified_at": data["modified_at"]}
        return data, source
