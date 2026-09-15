"""Scoped read-only tokens and snapshot pagination for recipient history."""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools.computer_use.recipient_bridge import RecipientBridge

from tools.computer_use.recipient_contract import RecipientError, identifier
from tools.computer_use.recipient_history_sources import (
    CATALOG_CANDIDATES, CATALOG_RECORDS, HISTORY_BYTES, HOST_ID, PREFIX_BYTES, NativeHistory, messages,
)

TOKEN_SECONDS = 300
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
PAGE_CHARACTERS = 32000


def history_capabilities():
    return {"read_only": True, "sources": ["codex_native_history", "claude_session_history"],
            "availability": "probe_with_catalog", "default_limit": DEFAULT_LIMIT, "max_limit": MAX_LIMIT,
            "history_bytes": HISTORY_BYTES, "identity_prefix_bytes": PREFIX_BYTES,
            "message_characters": 8000, "page_characters": PAGE_CHARACTERS,
            "catalog_candidates": CATALOG_CANDIDATES, "catalog_records": CATALOG_RECORDS,
            "token_seconds": TOKEN_SECONDS}


def _limit(value):
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_LIMIT:
        raise RecipientError("invalid_limit", 400)
    return value


def _observed():
    return datetime.now(timezone.utc).isoformat()


class RecipientHistory:
    def __init__(self, bridge: RecipientBridge):
        self.bridge = bridge

    def _history_sources(self):
        return NativeHistory(self.bridge.desktop, self.bridge._claude_adapter, self.bridge.authorize)

    def _read_cursor(self, db, cursor, kind, binding):
        value = self.bridge._get(db, "cursor", identifier(cursor, "cursor"))
        if value is None:
            raise RecipientError("cursor_not_found", 404)
        if value["kind"] != kind or value["binding"] != binding:
            raise RecipientError("history_cursor_mismatch")
        if value["expires_at"] <= time.time():
            raise RecipientError("history_cursor_expired")
        return value

    def _write_cursor(self, db, *, kind, binding, expires_at, **value):
        cursor = secrets.token_urlsafe(32)
        self.bridge._put(db, "cursor", cursor, {"kind": kind, "binding": binding,
                                        "expires_at": expires_at, **value})
        return cursor

    def _history_identity(self, target):
        public = target["public"]
        return {**{key: public[key] for key in ("recipient_id", "app", "task_id")}, "host_id": HOST_ID}

    def _validate_history_target(self, target):
        if target.get("backend") == "native_history":
            if target["expires_at"] <= time.time():
                raise RecipientError("history_target_expired")
        elif target.get("backend") == "claude_peer":
            self.bridge._claude_adapter().select(target["identity"])
        elif target["public"]["proven_control"] == "ui_bridge":
            self.bridge._view(target)
        else:
            raise RecipientError("native_history_unavailable", 503)

    def _read_history_target(self, target, *, prefix_only=False):
        self.bridge.authorize()
        self._validate_history_target(target)
        sources = self._history_sources()
        try:
            record = sources.resolve(target)
            data, source = sources.read(record, prefix_only=prefix_only)
        except OSError as exc:
            raise RecipientError("native_history_unavailable", 503) from exc
        self._validate_history_target(target)
        self.bridge.authorize()
        return data, source

    def catalog(self, app=None, limit=DEFAULT_LIMIT, cursor=None):
        from tools.computer_use.recipient_bridge import capabilities

        limit = _limit(limit)
        if app is not None and app not in ("codex_desktop", "claude_code"):
            raise RecipientError("unsupported_application", 400)
        with self.bridge._locked() as db:
            if cursor is not None:
                page = self._read_cursor(db, cursor, "catalog", app)
            else:
                records, sources, truncated = self._history_sources().catalog(app)
                page: dict = {"records": records, "sources": sources, "truncated": truncated,
                        "offset": 0, "expires_at": time.time() + TOKEN_SECONDS}
            recipients = []
            offset = page["offset"]
            for record in page["records"][offset:offset + limit]:
                _, source = self._history_sources().read(record, prefix_only=True)
                token = secrets.token_urlsafe(32)
                public = {**record["public"], "target_token": token, "source": source}
                self.bridge._put(db, "target", token, {**record, "public": public, "expires_at": page["expires_at"]})
                recipients.append(public)
            offset += len(recipients)
            more = offset < len(page["records"])
            next_cursor = None
            if more:
                next_cursor = self._write_cursor(db, kind="catalog", binding=app,
                                                  records=page["records"], sources=page["sources"],
                                                  truncated=page["truncated"], offset=offset,
                                                  expires_at=page["expires_at"])
            self.bridge.authorize()
            return {"recipients": recipients, "sources": page["sources"], "next_cursor": next_cursor,
                    "truncated": page["truncated"] or more, "observed_at": _observed(),
                    "capabilities": capabilities()}

    def history(self, *, target_token, limit=DEFAULT_LIMIT, cursor=None):
        limit = _limit(limit)
        with self.bridge._locked() as db:
            target = self.bridge._target(db, target_token)
            page = self._read_cursor(db, cursor, "history", target_token) if cursor is not None else None
            data, source = self._read_history_target(target)
            if page is not None and page["fingerprint"] != data["fingerprint"]:
                raise RecipientError("history_snapshot_changed")
            items, truncated = messages(target["public"]["app"], data["data"], target["public"]["task_id"],
                                        data["offset"])
            offset = page["offset"] if page else 0
            selected, characters = [], 0
            for item in items[offset:offset + limit]:
                if characters + len(item["text"]) > PAGE_CHARACTERS:
                    break
                selected.append(item)
                characters += len(item["text"])
            offset += len(selected)
            more = offset < len(items)
            expires_at = page["expires_at"] if page else min(time.time() + TOKEN_SECONDS,
                                                           target.get("expires_at", float("inf")))
            next_cursor = self._write_cursor(db, kind="history", binding=target_token,
                                              fingerprint=data["fingerprint"], offset=offset,
                                              expires_at=expires_at) if more else None
            self.bridge.authorize()
            return {**self._history_identity(target), "source": source, "messages": selected,
                    "next_cursor": next_cursor, "truncated": truncated or more, "observed_at": _observed()}

    def status(self, *, target_token):
        with self.bridge._locked() as db:
            target = self.bridge._target(db, target_token)
            self._validate_history_target(target)
            result = {**self._history_identity(target), "status": "unknown", "completion_tracking": False,
                      "available": False, "reason": "external_completion_unverified"}
            try:
                _, source = self._read_history_target(target, prefix_only=True)
                result.update(available=True, source=source)
            except RecipientError as exc:
                if exc.status != 503:
                    raise
                result["reason"] = exc.code
            self.bridge.authorize()
            return {**result, "observed_at": _observed()}
