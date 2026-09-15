"""Authenticated existing-window delivery with durable, two-phase native receipts."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from tools.computer_use.recipient_contract import (
    RecipientError, app_identity, digest, identifier, recipient_view, window_identity,
)
from tools.computer_use.recipient_windows import WindowsRecipients
from tools.computer_use.recipient_lease import desktop_lease
from tools.computer_use.recipient_history import RecipientHistory, history_capabilities


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def authorization_guard(callback):
    def guard():
        try:
            return callback()
        except RecipientError:
            raise
        except Exception as exc:
            raise RecipientError("recipient_authority_revoked", 403) from exc
    return guard


def capabilities(computer_use=None):
    return {"version": 1, "operations": ["list", "select", "send", "reconcile", "inspect", "catalog", "history", "status"],
            "two_phase_send": True, "existing_tasks_only": True,
            "history": history_capabilities(),
            "task_identity": "application_deeplink_or_verified_process_session", "completion_tracking": False,
            "computer_use": computer_use or {"mode": "unknown", "verified": False,
                                           "tool": "inspect_screen", "reason": "host_not_probed"}}


class RecipientBridge:
    def __init__(self, *, session_id, owner, state_dir=None, desktop=None, claude=None, authorize=None):
        from hermes_constants import get_hermes_home
        self.scope = digest([identifier(session_id, "session_id"), identifier(owner, "owner")])
        self.root = Path(state_dir) if state_dir else get_hermes_home() / "recipient-bridge"
        self.root.mkdir(parents=True, exist_ok=True)
        self.desktop = desktop or WindowsRecipients()
        self.claude = claude
        self.authorize = authorization_guard(authorize or (lambda: None))

    @contextmanager
    def _locked(self):
        # A separate lease database permits receipt commits without releasing the UI lease.
        # Every bridge in this profile shares the lease, including different API peers.
        with desktop_lease(), closing(sqlite3.connect(self.root / "desktop-lease.sqlite3", timeout=25)) as lease:
            lease.execute("BEGIN IMMEDIATE")
            with closing(sqlite3.connect(self.root / "receipts.sqlite3", timeout=25)) as db:
                db.execute("CREATE TABLE IF NOT EXISTS records "
                           "(scope TEXT, kind TEXT, key TEXT, value TEXT, PRIMARY KEY(scope,kind,key))")
                db.commit()
                self.authorize()
                yield db

    def _get(self, db, kind, key):
        row = db.execute("SELECT value FROM records WHERE scope=? AND kind=? AND key=?",
                         (self.scope, kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def _put(self, db, kind, key, value):
        db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?)",
                   (self.scope, kind, key, json.dumps(value, ensure_ascii=False)))
        db.commit()

    def _target(self, db, token):
        value = self._get(db, "target", identifier(token, "target_token"))
        if value is None:
            raise RecipientError("recipient_not_found", 404)
        return value

    def _claude_adapter(self):
        if self.claude is None:
            from tools.computer_use.recipient_claude import ClaudeRecipients
            try:
                self.claude = ClaudeRecipients()
            except ImportError as exc:
                raise RecipientError("claude_native_dependency_unavailable", 503) from exc
        return self.claude

    def _snapshot(self, target):
        if target.get("backend") in {"claude_peer", "native_history"}:
            raise RecipientError("recipient_inspection_unavailable")
        snapshot = self.desktop.snapshot(target["identity"])
        if window_identity(snapshot["window"]) != window_identity(target["identity"]):
            raise RecipientError("recipient_window_changed")
        return snapshot

    def _view(self, target):
        view = recipient_view(self._snapshot(target), self.desktop.profiles.get(target["public"]["app"]))
        if view["identity"] != target["identity"]:
            raise RecipientError("recipient_task_changed")
        return view

    def probe(self):
        self.authorize()
        computer = self.desktop.computer_use_capability()
        return {"recipient_bridge": capabilities(computer), **self.desktop.probe(),
                "computer_use": computer}

    def list_recipients(self, app=None):
        if app is not None and app not in {"codex_desktop", "claude_code"}:
            raise RecipientError("unsupported_application", 400)
        recipients = []
        with self._locked() as db:
            computer = self.desktop.computer_use_capability()
            can_capture = computer["mode"] == "delegated" and computer["verified"] is True
            for window in ([] if app == "claude_code" else self.desktop.windows()):
                actual_app = app_identity(window)
                if actual_app != "codex_desktop" or (app and app != actual_app):
                    continue
                identity = window_identity(window)
                reason, control = None, "none"
                try:
                    snapshot = self.desktop.snapshot(identity)
                    view = recipient_view(snapshot, self.desktop.profiles.get(actual_app))
                    identity, control = view["identity"], "ui_bridge"
                except RecipientError as exc:
                    reason = exc.code
                token = secrets.token_urlsafe(32)
                public = {"recipient_id": "recipient:" + digest(identity)[:32],
                          "app": actual_app, "task_id": identity.get("task_id", "window:" + digest(identity)[:32]),
                          "title": window.get("title", actual_app), "target_token": token,
                          "proven_control": control,
                          "operations": ["select"] + (["inspect"] if can_capture else [])
                          + (["send", "reconcile", "history", "status"] if control == "ui_bridge" else []),
                          "reason": reason}
                self._put(db, "target", token, {"identity": identity, "public": public})
                recipients.append(public)
            peer_reason = None
            if app in (None, "claude_code"):
                try:
                    records = self._claude_adapter().list_recipients()
                except RecipientError as exc:
                    records, peer_reason = [], exc.code
                for record in records:
                    token = secrets.token_urlsafe(32)
                    public = {**record["public"], "target_token": token,
                              "operations": [*record["public"]["operations"], "history", "status"]}
                    self._put(db, "target", token, {**record, "public": public, "backend": "claude_peer"})
                    recipients.append(public)
        result = {"recipients": recipients, "capabilities": capabilities(computer)}
        if peer_reason:
            result["capabilities"]["claude_peer_reason"] = peer_reason
        return result

    def select(self, target_token):
        with self._locked() as db:
            target = self._target(db, target_token)
            if target.get("backend") == "native_history":
                RecipientHistory(self)._read_history_target(target, prefix_only=True)
            elif target.get("backend") == "claude_peer":
                self._claude_adapter().select(target["identity"])
            elif target["public"]["proven_control"] == "ui_bridge":
                self._view(target)
            else:
                self._snapshot(target)
            return {**target["public"], "selected": True, "status": "selected"}

    def catalog(self, app=None, limit=20, cursor=None):
        return RecipientHistory(self).catalog(app=app, limit=limit, cursor=cursor)

    def history(self, *, target_token, limit=20, cursor=None):
        return RecipientHistory(self).history(target_token=target_token, limit=limit, cursor=cursor)

    def status(self, *, target_token):
        return RecipientHistory(self).status(target_token=target_token)

    @staticmethod
    def _receipt(operation):
        private = {"message", "target_token", "baseline"}
        result = {key: value for key, value in operation.items() if key not in private}
        result["submission_attempted"] = bool(operation.get("attempted_at"))
        if operation["status"] == "posted":
            result["delivery_stage"] = "posted"
        elif result["submission_attempted"]:
            result["delivery_stage"] = "submission_attempted"
        elif operation["status"] == "queued":
            result["delivery_stage"] = "prepared"
        else:
            result["delivery_stage"] = operation["status"]
        if result["status"] == "preparing":
            result["status"] = "unknown"
        return result

    def _reconcile(self, db, operation, target):
        if operation["status"] in {"posted", "failed"}:
            return self._receipt(operation)
        self.authorize()
        if target.get("backend") == "claude_peer":
            if operation["status"] != "queued":
                receipt = self._claude_adapter().reconcile(
                    target["identity"], digest([self.scope, operation["operation_id"]]),
                    message=operation["message"])
                self._peer_result(operation, receipt)
                self._put(db, "operation", operation["operation_id"], operation)
            return self._receipt(operation)
        try:
            view = self._view(target)
            if operation["status"] == "preparing":
                if view["composer_text"] == operation["message"]:
                    operation.update(status="queued", commit_token=secrets.token_urlsafe(32))
                else:
                    operation.update(status="failed", reason="prepare_not_verified")
            elif operation["status"] == "unknown":
                ids = [m["id"] for m in view["messages"]]
                anchor = operation["baseline"][-1] if operation["baseline"] else None
                tail = view["messages"][ids.index(anchor) + 1:] if anchor in ids else []
                matches = [m for m in tail if m["id"] not in operation["baseline"]
                           and m["text"] == operation["message"]]
                receipt = None
                if len(matches) == 1:
                    receipt = {"native_message_id": matches[0]["id"],
                               "sha256": hashlib.sha256(operation["message"].encode()).hexdigest()}
                elif operation.get("attempted_at"):
                    observer = getattr(self.desktop, "posted_receipt", None)
                    if observer is not None:
                        receipt = observer(target["identity"], operation["message"],
                                           operation["attempted_at"], view["messages"])
                if receipt is not None:
                    self.authorize()
                    operation.update(status="posted", posted_at=timestamp(), message_receipt=receipt)
        except RecipientError:
            # Identity loss after a possibly successful invoke can never authorize a retry.
            pass
        self._put(db, "operation", operation["operation_id"], operation)
        return self._receipt(operation)

    @staticmethod
    def _peer_result(operation, receipt):
        status = receipt.get("status", "unknown")
        operation["status"] = status if status in {"posted", "failed", "unknown"} else "unknown"
        for field in ("reason", "message_receipt"):
            if field in receipt:
                operation[field] = receipt[field]
        if status == "posted":
            operation["posted_at"] = timestamp()

    def _send_peer(self, db, target, operation_id, target_token, message, commit_token):
        adapter = self._claude_adapter()
        operation = self._get(db, "operation", operation_id)
        if operation is None:
            if commit_token is not None:
                raise RecipientError("operation_not_found", 404)
            adapter.select(target["identity"])
            self.authorize()
            operation = {key: target["public"][key] for key in ("recipient_id", "app", "task_id")}
            operation.update(operation_id=operation_id, target_token=target_token, message=message,
                             status="queued", commit_token=secrets.token_urlsafe(32), created_at=timestamp())
            self._put(db, "operation", operation_id, operation)
            return self._receipt(operation)
        if operation["target_token"] != target_token or operation["message"] != message:
            raise RecipientError("operation_conflict")
        if operation["status"] != "queued":
            return self._reconcile(db, operation, target)
        if commit_token is None:
            return self._receipt(operation)
        if not isinstance(commit_token, str) or not hmac.compare_digest(commit_token, operation["commit_token"]):
            raise RecipientError("invalid_commit_token", 403)
        adapter.select(target["identity"])
        self.authorize()
        operation.update(status="unknown", attempted_at=timestamp())
        operation.pop("commit_token", None)
        self._put(db, "operation", operation_id, operation)
        try:
            receipt = adapter.send(target["identity"], message, digest([self.scope, operation_id]),
                                   authorize=self.authorize)
            self._peer_result(operation, receipt)
            self._put(db, "operation", operation_id, operation)
        except RecipientError as exc:
            if exc.status in {401, 403}:
                raise
        except Exception:
            # The peer might have consumed the message before the connection failed.
            pass
        return self._reconcile(db, operation, target)

    def send(self, *, operation_id, target_token, message, commit_token=None):
        operation_id = identifier(operation_id, "operation_id")
        if not isinstance(message, str) or not message.strip() or len(message) > 16000 or "\x00" in message:
            raise RecipientError("invalid_message", 400)
        with self._locked() as db:
            target = self._target(db, target_token)
            if target.get("backend") == "claude_peer":
                return self._send_peer(db, target, operation_id, target_token, message, commit_token)
            operation = self._get(db, "operation", operation_id)
            if operation is not None:
                if operation["target_token"] != target_token or operation["message"] != message:
                    raise RecipientError("operation_conflict")
                if operation["status"] != "queued":
                    return self._reconcile(db, operation, target)
                if commit_token is None:
                    return self._receipt(operation)
                if not isinstance(commit_token, str) or not hmac.compare_digest(commit_token, operation["commit_token"]):
                    raise RecipientError("invalid_commit_token", 403)
                view = self._view(target)
                if view["composer_text"] != message or not view["submit_id"]:
                    raise RecipientError("composer_or_submit_changed")
                self.authorize()
                operation.update(status="unknown", attempted_at=timestamp())
                operation.pop("commit_token", None)
                self._put(db, "operation", operation_id, operation)
                try:
                    self.desktop.submit(target["identity"], message, view["submit_id"], authorize=self.authorize)
                except RecipientError as exc:
                    if exc.status in (401, 403):
                        raise
                except Exception:
                    # Even a timeout may have invoked the button. Reconciliation is read-only.
                    pass
                return self._reconcile(db, operation, target)

            if commit_token is not None:
                raise RecipientError("operation_not_found", 404)
            if target["public"]["proven_control"] != "ui_bridge":
                raise RecipientError("recipient_control_unavailable")
            view = self._view(target)
            if view["composer_text"] != "":
                raise RecipientError("composer_not_empty")
            operation = {key: target["public"][key] for key in ("recipient_id", "app", "task_id")}
            operation.update(operation_id=operation_id, target_token=target_token, message=message,
                             baseline=[m["id"] for m in view["messages"]], status="preparing", created_at=timestamp())
            self._put(db, "operation", operation_id, operation)
            self.authorize()
            try:
                self.desktop.compose(target["identity"], message, authorize=self.authorize)
            except RecipientError as exc:
                if exc.status in (401, 403):
                    raise
            except Exception:
                pass
            return self._reconcile(db, operation, target)

    def reconcile(self, *, operation_id, target_token):
        with self._locked() as db:
            target = self._target(db, target_token)
            operation = self._get(db, "operation", identifier(operation_id, "operation_id"))
            if operation is None:
                raise RecipientError("operation_not_found", 404)
            if operation["target_token"] != target_token:
                raise RecipientError("operation_conflict")
            return self._reconcile(db, operation, target)

    def inspect(self, *, target_token, capture=False):
        if not isinstance(capture, bool):
            raise RecipientError("invalid_capture", 400)
        with self._locked() as db:
            target = self._target(db, target_token)
            snapshot = self._snapshot(target)
            if target["public"]["proven_control"] == "ui_bridge":
                self._view(target)
            result = {key: target["public"][key] for key in ("recipient_id", "app", "task_id")}
            result.update(status="completed", window=window_identity(snapshot["window"]),
                          title=snapshot["window"].get("title"), capture=capture)
            if not capture:
                return result
            self.authorize()
            image = self.desktop.capture(target["identity"])
            self.authorize()
            self._snapshot(target)
            if target["public"]["proven_control"] == "ui_bridge":
                self._view(target)
            try:
                payload = base64.b64decode(image.png_b64, validate=True)
            except (ValueError, TypeError) as exc:
                raise RecipientError("capture_not_verified") from exc
            from tools.computer_use.backend import image_dimensions_from_bytes
            if not image_dimensions_from_bytes(payload):
                raise RecipientError("capture_not_verified")
            mime = image.image_mime_type or ("image/png" if payload.startswith(b"\x89PNG") else "image/jpeg")
            if not payload or mime not in {"image/png", "image/jpeg"}:
                raise RecipientError("capture_not_verified")
            artifact_id, captured_at = secrets.token_hex(20), timestamp()
            directory = self.root / "captures" / self.scope
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (artifact_id + (".png" if mime == "image/png" else ".jpg"))
            path.write_bytes(payload)
            artifact = {"artifact_id": artifact_id, "captured_at": captured_at, "path": str(path.resolve()),
                        "mime_type": mime, "sha256": hashlib.sha256(payload).hexdigest(),
                        "window": result["window"], "recipient_id": result["recipient_id"], "task_id": result["task_id"]}
            return {**result, "artifact_id": artifact_id, "captured_at": captured_at, "artifact": artifact}
