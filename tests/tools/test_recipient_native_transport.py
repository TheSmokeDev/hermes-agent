"""The native subprocess waits for fresh authorization before any UI mutation."""
import io
import json

import pytest

from tools.computer_use.recipient_contract import RecipientError
from tools.computer_use.recipient_windows import WindowsRecipients


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
    monkeypatch.setattr(native.sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", "C:/Windows")
    monkeypatch.setattr(cua_backend, "sanitized_cua_driver_env", lambda: {})
    monkeypatch.setattr(native.subprocess, "Popen", lambda *a, **kw: process)
    def authorize():
        payload = json.loads(process.stdin.getvalue())
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
