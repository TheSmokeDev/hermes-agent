"""Canonical ownership of opt-in API run inputs; reservation is never execution permission."""
from __future__ import annotations

import json
import time

from hermes_state_passive_history import (
    PassiveHistoryConflictError, PassiveHistoryRetiredError, _payload_fingerprint,
    _validated_identifier, _validated_messages,
)

_ORIGIN_SQL = "SELECT * FROM execution_origins WHERE producer=? AND event_id=?"


def origin_metadata(claim):
    return {key: claim[key] for key in ("producer", "event_id", "origin_turn_id", "run_id")}


def validate_origin_input(origin, text):
    if not isinstance(origin, dict) or set(origin) != {"event_id", "origin_turn_id"}:
        raise ValueError("origin requires event_id and origin_turn_id")
    for key, value in origin.items():
        _validated_identifier(value, key, 128)
    _validated_messages([{"role": "user", "content": text}])


class SessionExecutionOriginsMixin:
    def reserve_execution_origin(self, session_id, *, producer, event_id, origin_turn_id,
                                 content, run_id, run_scope):
        validate_origin_input({"event_id": event_id, "origin_turn_id": origin_turn_id}, content)
        _validated_identifier(producer, "producer", 64)
        _validated_identifier(run_id, "run_id", 128)
        if not isinstance(run_scope, str) or not run_scope:
            raise ValueError("run_scope is required")
        fingerprint = _payload_fingerprint(origin_turn_id, [{"role": "user", "content": content}])

        def write(conn):
            existing = conn.execute(_ORIGIN_SQL, (producer, event_id)).fetchone()
            if existing is not None and existing["state"] == "retired":
                raise PassiveHistoryRetiredError("Execution origin is retired")
            owner = self._passive_conversation_id(conn, session_id)
            if existing is not None:
                if any(existing[key] != value for key, value in {
                    "conversation_id": owner, "origin_turn_id": origin_turn_id,
                    "payload_sha256": fingerprint, "run_id": run_id, "run_scope": run_scope,
                }.items()):
                    raise PassiveHistoryConflictError("Execution origin already has a different owner")
                return dict(existing)
            if conn.execute("SELECT 1 FROM passive_history_commits WHERE producer=? AND event_id=?",
                            (producer, event_id)).fetchone():
                raise PassiveHistoryConflictError("Origin was already committed passively")
            tip = self._resolve_passive_history_tip(conn, owner, requested_session_id=session_id)
            conn.execute(
                "INSERT INTO execution_origins (producer,event_id,origin_turn_id,payload_sha256,"
                "conversation_id,requested_session_id,session_id,run_id,run_scope,state,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'pending',?)",
                (producer, event_id, origin_turn_id, fingerprint, owner, session_id, tip,
                 run_id, run_scope, time.time()))
            return dict(conn.execute(_ORIGIN_SQL, (producer, event_id)).fetchone())
        return self._execute_write(write)

    def _verify_execution_origin_row(self, conn, origin):
        if origin["state"] == "retired":
            raise PassiveHistoryRetiredError("Execution origin is retired")
        row = conn.execute("SELECT session_id,role,content,display_metadata,platform_message_id "
                           "FROM messages WHERE id=?", (origin["message_id"],)).fetchone()
        expected = origin_metadata(origin)
        metadata = self._decode_display_metadata(row["display_metadata"]) if row else None
        if row is None or row["session_id"] != origin["session_id"] or row["role"] != "user" or (
            not isinstance(metadata, dict) or metadata.get("execution_origin") != expected
        ) or row["platform_message_id"] != "origin:" + origin["event_id"]:
            raise PassiveHistoryRetiredError("Execution origin canonical row is unavailable")
        content = self._decode_content(row["content"])
        if _payload_fingerprint(origin["origin_turn_id"], [{"role": "user", "content": content}]) != origin["payload_sha256"]:
            raise PassiveHistoryRetiredError("Execution origin canonical payload changed")

    def _prepare_execution_origin_batch(self, conn, session_id, messages, claim):
        """Return fresh rows plus the one pending origin row; leave retry rows stamped with their ID."""
        origin = conn.execute(_ORIGIN_SQL, (claim["producer"], claim["event_id"])).fetchone()
        if origin is None or origin["state"] == "retired":
            raise PassiveHistoryRetiredError("Execution origin reservation is unavailable")
        if any(origin[key] != claim[key] for key in (
            "origin_turn_id", "run_id", "run_scope", "payload_sha256", "conversation_id",
        )) or self._passive_conversation_id(conn, session_id) != origin["conversation_id"]:
            raise PassiveHistoryConflictError("Execution origin claim does not own this write")
        if self._resolve_passive_history_tip(
            conn, origin["conversation_id"], requested_session_id=origin["requested_session_id"],
        ) != session_id:
            raise PassiveHistoryConflictError("Execution origin write is not on its canonical segment")
        fresh, binding = [], None
        for message in messages:
            metadata = message.get("display_metadata")
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            marker = metadata.get("execution_origin") if isinstance(metadata, dict) else None
            if marker is None:
                fresh.append(message)
                continue
            if marker != origin_metadata(origin) or message.get("role") != "user" or (
                message.get("platform_message_id") != "origin:" + origin["event_id"]
            ) or _payload_fingerprint(origin["origin_turn_id"], [
                {"role": "user", "content": message.get("content")}
            ]) != origin["payload_sha256"]:
                raise PassiveHistoryConflictError("Execution origin row does not match its input")
            if origin["state"] == "adopted":
                self._verify_execution_origin_row(conn, origin)
                message["_row_id"] = origin["message_id"]
                continue
            if binding is not None:
                raise PassiveHistoryConflictError("An origin binds exactly one user row")
            message.pop("_row_id", None)
            binding = message
            fresh.append(message)
        return fresh, binding

    def _bind_execution_origin_row(self, conn, session_id, claim, message):
        if message is not None:
            conn.execute("UPDATE execution_origins SET state='adopted',message_id=?,session_id=? "
                         "WHERE producer=? AND event_id=? AND state='pending'",
                         (message["_row_id"], session_id, claim["producer"], claim["event_id"]))

    def adopt_execution_origin(self, session_id, *, producer, event_id, origin_turn_id, content):
        validate_origin_input({"event_id": event_id, "origin_turn_id": origin_turn_id}, content)
        fingerprint = _payload_fingerprint(origin_turn_id, [{"role": "user", "content": content}])
        with self._read_ctx() as conn:
            origin = conn.execute(_ORIGIN_SQL, (producer, event_id)).fetchone()
            if origin is not None and origin["state"] == "retired":
                raise PassiveHistoryRetiredError("Execution origin is retired")
            owner = self._passive_conversation_id(conn, session_id)
            if origin is None:
                return {"status": "pending", "reserved": False, "message_ids": []}
            if origin["conversation_id"] != owner or origin["payload_sha256"] != fingerprint or (
                origin["origin_turn_id"] != origin_turn_id
            ):
                raise PassiveHistoryConflictError("Execution origin payload or owner differs")
            if origin["state"] == "adopted":
                self._verify_execution_origin_row(conn, origin)
            return {"status": origin["state"], "reserved": True,
                    **{key: origin[key] for key in ("event_id", "origin_turn_id", "run_id", "conversation_id", "session_id")},
                    "message_ids": [origin["message_id"]] if origin["state"] == "adopted" else []}
