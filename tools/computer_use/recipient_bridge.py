"""Authenticated existing-window delivery with durable, two-phase native receipts."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from tools.computer_use.recipient_contract import (
    RecipientError, app_identity, digest, identifier, recipient_view, window_identity,
)
from tools.computer_use.recipient_windows import WindowsRecipients
from tools.computer_use.recipient_lease import desktop_lease


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


def capabilities():
    return {"version": 1, "operations": ["list", "select", "send", "reconcile", "inspect"],
            "two_phase_send": True, "existing_tasks_only": True,
            "task_identity": "native_window_and_pane", "completion_tracking": False}


class RecipientBridge:
    def __init__(self, *, session_id, owner, state_dir=None, desktop=None, authorize=None):
        from hermes_constants import get_hermes_home
        self.scope = digest([identifier(session_id, "session_id"), identifier(owner, "owner")])
        self.root = Path(state_dir) if state_dir else get_hermes_home() / "recipient-bridge"
        self.root.mkdir(parents=True, exist_ok=True)
        self.desktop = desktop or WindowsRecipients()
        self.authorize = authorization_guard(authorize or (lambda: None))

    @contextmanager
    def _locked(self):
        # A separate lease database permits receipt commits without releasing the UI lease.
        # Every bridge in this profile shares the lease, including different API peers.
        with desktop_lease(), sqlite3.connect(self.root / "desktop-lease.sqlite3", timeout=25) as lease:
            lease.execute("BEGIN IMMEDIATE")
            with sqlite3.connect(self.root / "receipts.sqlite3", timeout=25) as db:
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

    def _snapshot(self, target):
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
        from tools.computer_use.cua_backend import cua_driver_runtime_contract_status
        self.authorize()
        contract = cua_driver_runtime_contract_status()
        return {"recipient_bridge": capabilities(), **self.desktop.probe(),
                "computer_use": {"available": bool(contract.get("ready")),
                                 "reason": contract.get("reason"), "capture": "on_demand",
                                 "permissions_verified": False}}

    def list_recipients(self, app=None):
        if app is not None and app not in {"codex", "claude_code"}:
            raise RecipientError("unsupported_application", 400)
        recipients = []
        with self._locked() as db:
            for window in self.desktop.windows():
                actual_app = app_identity(window)
                if actual_app is None or (app and app != actual_app):
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
                          "operations": ["select", "inspect"] + (["send", "reconcile"] if control == "ui_bridge" else []),
                          "reason": reason}
                self._put(db, "target", token, {"identity": identity, "public": public})
                recipients.append(public)
        return {"recipients": recipients, "capabilities": capabilities()}

    def select(self, target_token):
        with self._locked() as db:
            target = self._target(db, target_token)
            if target["public"]["proven_control"] == "ui_bridge":
                self._view(target)
            else:
                self._snapshot(target)
            return {**target["public"], "selected": True}

    @staticmethod
    def _receipt(operation):
        private = {"message", "target_token", "baseline"}
        result = {key: value for key, value in operation.items() if key not in private}
        if result["status"] == "preparing":
            result["status"] = "unknown"
        return result

    def _reconcile(self, db, operation, target):
        if operation["status"] in {"posted", "failed"}:
            return self._receipt(operation)
        self.authorize()
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
                if len(matches) == 1:
                    operation.update(status="posted", posted_at=timestamp(),
                                     message_receipt={"native_message_id": matches[0]["id"],
                                                      "sha256": hashlib.sha256(operation["message"].encode()).hexdigest()})
        except RecipientError:
            # Identity loss after a possibly successful invoke can never authorize a retry.
            pass
        self._put(db, "operation", operation["operation_id"], operation)
        return self._receipt(operation)

    def send(self, *, operation_id, target_token, message, commit_token=None):
        operation_id = identifier(operation_id, "operation_id")
        if not isinstance(message, str) or not message.strip() or len(message) > 16000 or "\x00" in message:
            raise RecipientError("invalid_message", 400)
        with self._locked() as db:
            target = self._target(db, target_token)
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
