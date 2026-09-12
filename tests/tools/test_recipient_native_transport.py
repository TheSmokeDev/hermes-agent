"""The native subprocess waits for fresh authorization before any UI mutation."""
import io
import json

import pytest

from tools.computer_use.recipient_contract import RecipientError
from tools.computer_use.recipient_windows import WindowsRecipients


@pytest.mark.windows_only
@pytest.mark.parametrize("revoked", [False, True])
def test_native_ready_handshake_checks_authority_before_commit(monkeypatch, revoked):
    import tools.computer_use.recipient_windows as native
    from tools.computer_use import cua_backend

    class Process:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO('{"ready":true}\n')
            self.returncode = None
            self.committed = False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def poll(self):
            return self.returncode
        def communicate(self, value, timeout):
            assert authorized[0] and value == "commit\n"
            self.committed = True
            self.returncode = 0
            return '{"submitted":true}', ""
        def kill(self):
            self.returncode = -9
        def wait(self, timeout):
            return self.returncode

    process, authorized = Process(), [False]
    monkeypatch.setenv("SystemRoot", "C:/Windows")
    monkeypatch.setattr(cua_backend, "sanitized_cua_driver_env", lambda: {})
    monkeypatch.setattr(native.subprocess, "Popen", lambda *a, **kw: process)
    def authorize():
        payload = json.loads(process.stdin.getvalue().splitlines()[1])
        assert payload["action"] == "submit" and payload["target"]["composer_id"] == "exact-editor"
        if revoked:
            raise RecipientError("revoked", 403)
        authorized[0] = True
    desktop = WindowsRecipients(profiles={})
    if revoked:
        with pytest.raises(RecipientError, match="revoked"):
            desktop.submit({"composer_id": "exact-editor"}, "hello", "exact-send", authorize=authorize)
        assert not process.committed
    else:
        assert desktop.submit({"composer_id": "exact-editor"}, "hello", "exact-send",
                              authorize=authorize) == {"submitted": True}
        assert process.committed


@pytest.mark.windows_only
def test_null_native_control_types_have_no_action_role():
    import base64
    import os
    from pathlib import Path
    import subprocess
    from hermes_cli._subprocess_compat import windows_hide_flags
    from tools.computer_use.recipient_windows_script import CONTROL_ROLE_SCRIPT
    command = CONTROL_ROLE_SCRIPT + """
$roles=@((ControlRole $null),(ControlRole ([pscustomobject]@{ProgrammaticName=$null})),(ControlRole ([pscustomobject]@{ProgrammaticName='ControlType.Edit'})))
ConvertTo-Json -InputObject $roles -Compress
"""
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand",
                             base64.b64encode(command.encode("utf-16le")).decode()], capture_output=True,
                            text=True, encoding="utf-8", timeout=10, creationflags=windows_hide_flags())
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["Unknown", "Unknown", "Edit"]
