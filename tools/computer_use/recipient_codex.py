"""Read-only posted-message evidence for an already verified live Codex task."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
import tomllib

import psutil

from tools.computer_use.recipient_contract import app_identity


def _native_context(target):
    process = psutil.Process(target["pid"])
    started = datetime.fromisoformat(target["process_started"].replace("Z", "+00:00")).timestamp()
    if (app_identity(target) != "codex_desktop"
            or os.path.normcase(process.exe()) != os.path.normcase(target["exe"])
            or abs(process.create_time() - started) > 0.000001
            or process.username().casefold() != psutil.Process().username().casefold()):
        return None
    environment = process.environ()
    if not environment.get("CODEX_HOME") and not environment.get("USERPROFILE"):
        return None
    home = Path(environment.get("CODEX_HOME") or Path(environment["USERPROFILE"]) / ".codex")
    if not home.is_absolute():
        return None
    home = home.resolve()
    config = home / "config.toml"
    settings = tomllib.loads(config.read_text(encoding="utf-8")) if config.is_file() else {}
    sqlite_home = Path(environment.get("CODEX_SQLITE_HOME") or settings.get("sqlite_home") or home)
    if not sqlite_home.is_absolute():
        return None
    return home, sqlite_home.resolve()


def _time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("native timestamp has no timezone")
    return result.astimezone(timezone.utc)


def _editor_content_matches(native, expected):
    if native == expected:
        return True
    # Observed single-line literal serialization; preserve all original whitespace.
    if any(character in expected for character in ("\\", "\r", "\n")):
        return False
    return native == expected.replace("_", "\\_") + "\n"


def posted_receipt(target, message, attempted_at, visible_messages):
    if sum(item.get("text") == message for item in visible_messages) != 1:
        return None
    try:
        context = _native_context(target)
        if context is None:
            return None
        home, sqlite_home = context
        with closing(sqlite3.connect((sqlite_home / "state_5.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
            query = "SELECT rollout_path,source FROM threads WHERE id=?"
            row = db.execute(query, (target["task_id"],)).fetchone()
            if row is None or row[1] != "vscode":
                return None
            path = Path(row[0]).resolve(strict=True)
            if (not path.is_relative_to((home / "sessions").resolve()) or path.suffix != ".jsonl"
                    or path.stat().st_size > 64 * 1024 * 1024):
                return None
            after, now = _time(attempted_at), datetime.now(timezone.utc)
            matches, seen = [], set()
            with path.open("rb") as stream:
                first = json.loads(stream.readline())
                meta = first.get("payload", {})
                if (first.get("type") != "session_meta" or meta.get("id") != target["task_id"]
                        or meta.get("originator") != "Codex Desktop" or meta.get("source") != "vscode"):
                    return None
                for line in stream:
                    if not line.endswith(b"\n"):
                        break
                    record = json.loads(line)
                    item = record.get("payload", {})
                    if (record.get("type") != "response_item" or item.get("type") != "message"
                            or item.get("role") != "user"):
                        continue
                    written = _time(record.get("timestamp", ""))
                    if written <= after or written > now:
                        continue
                    native_id = item.get("id", "")
                    if not re.fullmatch(r"msg_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", native_id):
                        continue
                    if native_id in seen:
                        return None
                    seen.add(native_id)
                    content = item.get("content", [])
                    if (len(content) == 1 and content[0].get("type") == "input_text"
                            and _editor_content_matches(content[0].get("text"), message)):
                        matches.append({"native_message_id": native_id,
                                        "sha256": hashlib.sha256(message.encode()).hexdigest(),
                                        "source": "codex_native_history", "observed_at": written.isoformat()})
            if db.execute(query, (target["task_id"],)).fetchone() != row or _native_context(target) != context:
                return None
            return matches[0] if len(matches) == 1 else None
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, ImportError, psutil.Error):
        return None
