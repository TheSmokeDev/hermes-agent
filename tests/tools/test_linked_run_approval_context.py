"""Only an exact live host-created API run transport can expose human approval."""
import contextvars

import pytest

from tools import approval, approval_context as context


def test_live_binding_is_callback_exact_scoped_and_revocable(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "api_server")
    token = context.set_current_session_key("owning-run")
    callback = lambda data: None
    foreign = lambda data: None
    approval.register_gateway_notify("owning-run", callback)
    try:
        # A registered callback alone does not change ordinary API unattended policy.
        assert not context._is_gateway_approval_context()
        assert not approval.request_tool_approval("write_file", "ordinary API request")["approved"]
        with pytest.raises(ValueError):
            with context.bind_api_run_approval_transport("owning-run", foreign):
                pass
        with context.bind_api_run_approval_transport("owning-run", callback):
            assert context._is_gateway_approval_context()
            inherited = contextvars.copy_context()
            other = context.set_current_session_key("other-run")
            try:
                assert not context._is_gateway_approval_context()
                assert not approval.request_tool_approval("write_file", "foreign run request")["approved"]
            finally:
                context.reset_current_session_key(other)
            monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webhook")
            assert not context._is_gateway_approval_context()
            monkeypatch.setenv("HERMES_SESSION_PLATFORM", "api_server")
            approval.register_gateway_notify("owning-run", foreign)
            assert not context._is_gateway_approval_context()
            approval.unregister_gateway_notify("owning-run")
            assert not inherited.run(context._is_gateway_approval_context)
            assert not approval.request_tool_approval("write_file", "revoked callback request")["approved"]
        forged = context._live_api_run_approval.set({"session_key": "owning-run", "callback": callback})
        try:
            approval.register_gateway_notify("owning-run", callback)
            assert not context._is_gateway_approval_context()
        finally:
            context._live_api_run_approval.reset(forged)
        assert not context._is_gateway_approval_context()
    finally:
        approval.unregister_gateway_notify("owning-run")
        context.reset_current_session_key(token)
