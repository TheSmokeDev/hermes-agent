"""Durable child-dispatch ownership over exact passive receipt-owned parent input."""
from __future__ import annotations

import time

from hermes_state_passive_history import (
    PassiveHistoryConflictError, PassiveHistoryRetiredError, _RECEIPT_ROW_SQL,
    _committed_message_ids, _payload_fingerprint, _validated_identifier, _validated_messages,
)


class SessionChildDispatchMixin:
    def bind_ordinary_steer_origin(self, session_id, *, producer, event_id, origin_turn_id, content,
                                   turn_lease_holder, target_guard, receipt_id=None):
        """Bind exact user input under the live run's existing lease; never accept client lease authority."""
        rows = _validated_messages([{"role": "user", "content": content}])
        for name, value in (("producer", producer), ("event_id", event_id), ("origin_turn_id", origin_turn_id)):
            _validated_identifier(value, name, 128)
        if not turn_lease_holder or (receipt_id is not None and (type(receipt_id) is not int or receipt_id < 1)):
            raise ValueError("A live lease and valid origin reference are required")
        def write(conn):
            if not target_guard():
                raise PassiveHistoryConflictError("Steering target changed before origin binding")
            self._check_transcript_write_guards(conn, session_id, None, turn_lease_holder=turn_lease_holder)
            owner = self._passive_conversation_id(conn, session_id)
            if self._resolve_passive_history_tip(conn, owner, requested_session_id=session_id) != session_id:
                raise PassiveHistoryConflictError("Steering must target the current canonical segment")
            user_id = self._child_origin_user(conn, session_id, producer, event_id, origin_turn_id, content, receipt_id)
            if user_id is None:
                receipt = self._append_passive_messages_on_conn(conn, session_id, producer=producer,
                    event_id=event_id, origin_turn_id=origin_turn_id, rows=rows,
                    fingerprint=_payload_fingerprint(origin_turn_id, rows), _turn_lease_holder=turn_lease_holder)
                user_id = receipt.message_ids[0]
            receipt = conn.execute(_RECEIPT_ROW_SQL, (producer, event_id)).fetchone()
            row = conn.execute("SELECT * FROM messages WHERE id=?", (user_id,)).fetchone()
            message = self._rows_to_conversation([row], session_id=session_id, include_ancestors=False,
                                                 repair_alternation=False, include_row_ids=True)[0]
            # This receipt-owned input was verified byte-for-byte above. Keep it
            # exact while retaining the persisted row markers and API sidecar.
            message["content"] = content
            if not message.get("api_content"):
                message["_exact_steer_leading"] = True
                message["_exact_steer_trailing"] = True
            return {"receipt_id": receipt["id"], "message": message}
        return self._execute_write(write, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)

    def verify_child_steer_origin(self, run_id, *, run_scope, child_id, content, event_id, origin_turn_id, receipt_id):
        """Reuse exact receipt-owned parent input without appending or searching by text."""
        with self._read_ctx() as conn:
            row = conn.execute("SELECT * FROM child_dispatches WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["state"] != "started" or row["run_scope"] != run_scope or row["child_id"] != child_id:
                raise PassiveHistoryRetiredError("Child dispatch is unavailable for this owner")
            return self._child_origin_user(conn, row["requested_session_id"], row["producer"], event_id,
                                           origin_turn_id, content, receipt_id)

    def _child_origin_user(self, conn, session_id, producer, event_id, origin_turn_id, content, receipt_id=None):
        receipt = conn.execute(_RECEIPT_ROW_SQL, (producer, event_id)).fetchone()
        if receipt is None:
            if receipt_id is not None:
                raise PassiveHistoryConflictError("Required passive origin receipt is unavailable")
            return None
        self._verify_passive_receipt(conn, receipt, producer=producer, event_id=event_id)
        owner = self._passive_conversation_id(conn, session_id)
        if receipt["conversation_id"] != owner or self._passive_conversation_id(conn, receipt["session_id"]) != owner or (
            receipt["origin_turn_id"] != origin_turn_id
        ) or (receipt_id is not None and receipt["id"] != receipt_id):
            raise PassiveHistoryConflictError("Child origin has a different owner or utterance")
        users = [conn.execute("SELECT id,role,content FROM messages WHERE id=?", (row_id,)).fetchone()
                 for row_id in _committed_message_ids(receipt["message_ids_json"])]
        users = [row for row in users if row is not None and row["role"] == "user"]
        if len(users) != 1 or self._decode_content(users[0]["content"]) != content:
            raise PassiveHistoryConflictError("Child origin must match its exact receipt-owned user row")
        return users[0]["id"]

    def prepare_child_dispatch(self, session_id, *, producer, event_id, origin_turn_id, content,
                               run_id, run_scope, correlation_id, fingerprint, receipt_id=None):
        rows = _validated_messages([{"role": "user", "content": content}])
        for key, value in {"event_id": event_id, "origin_turn_id": origin_turn_id,
                           "run_id": run_id, "correlation_id": correlation_id}.items():
            _validated_identifier(value, key, 128)
        _validated_identifier(producer, "producer", 64)
        if not run_scope or not fingerprint:
            raise ValueError("Child dispatch requires scoped durable request identity")
        if receipt_id is not None and (type(receipt_id) is not int or receipt_id < 1):
            raise ValueError("receipt_id must be a positive integer")

        def write(conn):
            if conn.execute("SELECT 1 FROM execution_origins WHERE producer=? AND event_id=?",
                            (producer, event_id)).fetchone():
                raise PassiveHistoryConflictError("Child dispatch supports fresh or passive origins only")
            owner = self._passive_conversation_id(conn, session_id)
            prior = conn.execute("SELECT * FROM child_dispatches WHERE run_id=? OR "
                                 "(conversation_id=? AND correlation_id=?)", (run_id, owner, correlation_id)).fetchone()
            if prior is not None:
                if prior["state"] == "retired":
                    raise PassiveHistoryRetiredError("Child dispatch is retired")
                if any(prior[key] != value for key, value in {
                    "run_id": run_id, "run_scope": run_scope, "fingerprint": fingerprint,
                    "producer": producer, "event_id": event_id, "origin_turn_id": origin_turn_id,
                    "conversation_id": owner, "correlation_id": correlation_id,
                }.items()):
                    raise PassiveHistoryConflictError("Child correlation already names another action")
                return dict(prior)
            tip = self._resolve_passive_history_tip(conn, owner, requested_session_id=session_id)
            user_id = self._child_origin_user(conn, session_id, producer, event_id, origin_turn_id, content, receipt_id)
            if user_id is None:
                receipt = self._append_passive_messages_on_conn(
                    conn, session_id, producer=producer, event_id=event_id, origin_turn_id=origin_turn_id,
                    rows=rows, fingerprint=_payload_fingerprint(origin_turn_id, rows))
                user_id = receipt.message_ids[0]
            conn.execute("INSERT INTO child_dispatches (run_id,run_scope,fingerprint,correlation_id,producer,"
                         "event_id,origin_turn_id,conversation_id,requested_session_id,parent_session_id,"
                         "parent_message_id,state,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'admitted',?)",
                         (run_id, run_scope, fingerprint, correlation_id, producer, event_id, origin_turn_id,
                          owner, session_id, tip, user_id, time.time()))
            return dict(conn.execute("SELECT * FROM child_dispatches WHERE run_id=?", (run_id,)).fetchone())
        return self._execute_write(write, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)

    def child_dispatch_is_current(self, run_id, *, run_scope, child_id, lease_holder=None):
        """Read-only ownership proof for a plugin worker; deletion/retirement revokes it."""
        with self._read_ctx() as conn:
            current = conn.execute(
                "SELECT 1 FROM child_dispatches d JOIN sessions p ON p.id=d.parent_session_id "
                "JOIN sessions c ON c.id=d.child_session_id JOIN messages m ON m.id=d.parent_message_id "
                "WHERE d.run_id=? AND d.run_scope=? AND d.child_id=? AND d.state='started'",
                (run_id, run_scope, child_id)).fetchone() is not None
            if not current or lease_holder is None:
                return current
            owner = self._session_turn_lease_key_on_conn(conn, child_id)
            lease = conn.execute("SELECT holder,expires_at FROM session_turn_leases WHERE conversation_id=?",
                                 (owner,)).fetchone()
            return lease is not None and lease["holder"] == lease_holder and lease["expires_at"] > time.time()

    def claim_child_dispatch(self, dispatch):
        def write(conn):
            row = conn.execute("SELECT * FROM child_dispatches WHERE run_id=?", (dispatch["run_id"],)).fetchone()
            if row is None or row["state"] == "retired":
                raise PassiveHistoryRetiredError("Child dispatch authorization was removed")
            if row["state"] != "admitted" or any(row[key] != dispatch[key] for key in (
                "run_scope", "fingerprint", "correlation_id", "conversation_id", "parent_message_id",
            )):
                raise PassiveHistoryConflictError("Child dispatch was already consumed or differs")
            user = conn.execute("SELECT content FROM messages WHERE id=?", (row["parent_message_id"],)).fetchone()
            if user is None:
                raise PassiveHistoryRetiredError("Child origin row was removed")
            verified = self._child_origin_user(conn, row["requested_session_id"], row["producer"], row["event_id"],
                                               row["origin_turn_id"], self._decode_content(user["content"]))
            if verified != row["parent_message_id"]:
                raise PassiveHistoryConflictError("Child origin receipt no longer owns the linked row")
            tip = self._resolve_passive_history_tip(
                conn, row["conversation_id"], requested_session_id=row["requested_session_id"])
            if tip != row["parent_session_id"]:
                raise PassiveHistoryConflictError("Child parent changed before dispatch")
            conn.execute("UPDATE child_dispatches SET state='launching' WHERE run_id=?", (row["run_id"],))
        return self._execute_write(write)

    def record_child_dispatch_handle(self, dispatch, *, child_id, child_session_id):
        def write(conn):
            changed = conn.execute("UPDATE child_dispatches SET state='started',child_id=?,child_session_id=? "
                                   "WHERE run_id=? AND run_scope=? AND state='launching'",
                                   (child_id, child_session_id, dispatch["run_id"], dispatch["run_scope"])).rowcount
            if changed != 1:
                raise PassiveHistoryRetiredError("Child dispatch was invalidated during launch")
        return self._execute_write(write)
