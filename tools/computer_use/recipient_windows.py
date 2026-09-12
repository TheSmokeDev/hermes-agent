"""Windows accessibility transport and explicitly requested computer-use captures."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from tools.computer_use.recipient_contract import RecipientError


class WindowsRecipients:
    def __init__(self, *, profiles=None):
        if profiles is None:
            from tools.computer_use.cua_backend import _computer_use_cfg
            profiles = _computer_use_cfg().get("recipient_profiles", {})
        self.profiles = profiles if isinstance(profiles, dict) else {}

    def probe(self):
        return {"native_accessibility": sys.platform == "win32", "reason":
                None if sys.platform == "win32" else "unsupported_platform"}

    def computer_use_capability(self):
        from tools.computer_use.cua_backend import cua_driver_runtime_contract_status
        try:
            contract = cua_driver_runtime_contract_status()
        except Exception:
            return {"mode": "unknown", "verified": False, "tool": "inspect_screen",
                    "reason": "selected_host_runtime_probe_failed"}
        ready = bool(contract.get("ready"))
        return {"mode": "delegated" if ready else "unavailable", "verified": True,
                "tool": "inspect_screen", "reason": "selected_host_runtime_ready_capture_not_run"
                if ready else "selected_host_computer_use_runtime_unavailable"}

    def _call(self, action, *, authorize=None, **arguments):
        if sys.platform != "win32":
            raise RecipientError("unsupported_platform")
        from tools.computer_use.recipient_windows_script import SCRIPT
        from hermes_cli._subprocess_compat import windows_hide_flags
        from tools.computer_use.cua_backend import sanitized_cua_driver_env
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        bootstrap = ("[Console]::InputEncoding=New-Object System.Text.UTF8Encoding($false);"
                     "Invoke-Expression ([Text.Encoding]::Unicode.GetString("
                     "[Convert]::FromBase64String([Console]::In.ReadLine())))")
        encoded = base64.b64encode(bootstrap.encode("utf-16le")).decode("ascii")
        argv = [str(powershell), "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand", encoded]
        options = dict(text=True, encoding="utf-8", errors="replace",
                       creationflags=windows_hide_flags(), env=sanitized_cua_driver_env())
        payload = (base64.b64encode(SCRIPT.encode("utf-16le")).decode("ascii") + "\n"
                   + json.dumps({"action": action, **arguments}, ensure_ascii=False) + "\n")
        try:
            if action in {"compose", "submit"}:
                if authorize is None:
                    raise RecipientError("native_authorization_required", 403)
                with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, **options) as process:
                    watchdog = threading.Timer(25, process.kill)
                    watchdog.start()
                    try:
                        process.stdin.write(payload)
                        process.stdin.flush()
                        ready = json.loads(process.stdout.readline())
                        if ready != {"ready": True}:
                            raise RecipientError("native_accessibility_refused")
                        authorize()
                        output, _ = process.communicate("commit\n", timeout=20)
                        value, returncode = json.loads(output), process.returncode
                    finally:
                        watchdog.cancel()
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
            else:
                result = subprocess.run(argv, input=payload, capture_output=True, timeout=20, **options)
                value, returncode = json.loads(result.stdout), result.returncode
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            if isinstance(exc, RecipientError):
                raise
            raise RecipientError("native_accessibility_unknown") from exc
        if returncode or value.get("error"):
            code = value.get("error")
            known = {"recipient_window_changed", "recipient_control_stale", "recipient_pane_changed",
                     "wrong_composer", "composer_not_editable", "composer_text_changed",
                     "approval_surface_active", "submit_control_changed", "submit_control_unsupported",
                     "accessibility_truncated", "unsupported_accessibility", "recipient_task_changed", "task_deeplink_unavailable",
                     "clipboard_busy", "clipboard_changed", "clipboard_format_not_preservable",
                     "clipboard_backup_failed", "clipboard_restore_failed", "clipboard_owner_unavailable",
                     "task_deeplink_ambiguous", "task_deeplink_unavailable_Do_anything",
                     "task_deeplink_unavailable_Chat_actions", "task_deeplink_unavailable_Copy",
                     "task_deeplink_unavailable_Copy_deeplink_Alt+Ctrl+L"}
            raise RecipientError(code if code in known else "native_accessibility_refused")
        return value

    def windows(self):
        return self._call("windows")["windows"]

    def snapshot(self, target):
        from tools.computer_use.recipient_lease import desktop_lease
        with desktop_lease():
            try:
                return self._call("snapshot", target=target)
            except RecipientError as exc:
                if exc.code != "native_accessibility_refused":
                    raise
                return self._call("snapshot", target=target)

    def compose(self, target, message, *, authorize):
        return self._call("compose", target=target, message=message, authorize=authorize)

    def submit(self, target, message, submit_id, *, authorize):
        return self._call("submit", target=target, message=message, submit_id=submit_id, authorize=authorize)

    def posted_receipt(self, target, message, attempted_at, visible_messages):
        from tools.computer_use.recipient_codex import posted_receipt
        return posted_receipt(target, message, attempted_at, visible_messages)

    def capture(self, target):
        from tools.computer_use.cua_backend import CuaDriverBackend, cua_driver_runtime_contract_status
        if not cua_driver_runtime_contract_status().get("ready"):
            raise RecipientError("computer_use_unavailable", 503)
        # Private instance: unrelated computer_use sessions cannot change its sticky target.
        backend = CuaDriverBackend(permission_mode="standard")
        try:
            backend.start()
            return backend.capture(mode="vision", pid=target["pid"], window_id=target["window_id"])
        finally:
            backend.stop()
