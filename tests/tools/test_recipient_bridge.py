"""Behavioral tests for native recipient identity, two-phase send and receipts."""
import base64
import copy
import threading
from types import SimpleNamespace

import pytest

from tools.computer_use.recipient_bridge import RecipientBridge
from tools.computer_use.recipient_contract import RecipientError, recipient_view

WINDOW = {"pid": 71, "window_id": 9, "exe": "C:/Apps/Codex.exe", "process_started": "started",
          "product": "Codex", "company": "OpenAI OpCo, LLC", "title": "same title"}


def snapshot():
    return {"window": dict(WINDOW), "nodes": [
        {"id": "pane", "parent": "root", "role": "Group"},
        {"id": "editor", "parent": "pane", "role": "Edit", "name": "Do anything",
         "value_supported": True, "enabled": True, "value": ""},
        {"id": "send", "parent": "pane", "role": "Button", "name": "Send",
         "invoke_supported": True, "enabled": True},
        {"id": "old", "parent": "pane", "role": "Text", "name": "You said:"},
        {"id": "old-content", "parent": "pane", "role": "Text", "name": "prior",
         "text_supported": True},
        {"id": "assistant", "parent": "pane", "role": "Text", "name": "ChatGPT said:"},
    ]}


class Desktop:
    profiles = {}

    def __init__(self):
        self.state = snapshot()
        self.composes = self.submits = self.captures = 0
        self.lose_prepare = self.lose_submit = self.no_post = False

    def windows(self):
        return [dict(self.state["window"])]

    def snapshot(self, target):
        return copy.deepcopy(self.state)

    def compose(self, target, message, *, authorize):
        authorize()
        self.composes += 1
        self.state["nodes"][1]["value"] = message
        if self.lose_prepare:
            raise TimeoutError()

    def submit(self, target, message, submit_id, *, authorize):
        authorize()
        self.submits += 1
        if not self.no_post:
            self.state["nodes"].extend([
                {"id": "new", "parent": "pane", "role": "Text", "name": "You said:"},
                {"id": "new-content", "parent": "pane", "role": "Text", "name": message,
                 "text_supported": True},
                {"id": "next-assistant", "parent": "pane", "role": "Text", "name": "ChatGPT said:"},
            ])
            self.state["nodes"][1]["value"] = ""
        if self.lose_submit:
            raise TimeoutError()

    def capture(self, target):
        self.captures += 1
        return SimpleNamespace(png_b64=base64.b64encode(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"\x00\x00\x00\x01" * 2).decode(), image_mime_type="image/png")


def bridge(tmp_path, desktop=None, owner="owner", authorize=None):
    return RecipientBridge(session_id="session", owner=owner, state_dir=tmp_path,
                           desktop=desktop or Desktop(), authorize=authorize)


def selected(service):
    return service.list_recipients()["recipients"][0]["target_token"]


def prepare(service, token, message="requested message"):
    return service.send(operation_id="operation", target_token=token, message=message)


def commit(service, token, receipt, message="requested message"):
    return service.send(operation_id="operation", target_token=token, message=message,
                        commit_token=receipt["commit_token"])


def test_two_phase_send_and_restart_reconciliation(tmp_path):
    desktop = Desktop()
    service = bridge(tmp_path, desktop)
    token = selected(service)
    queued = prepare(service, token)
    assert queued["status"] == "queued" and desktop.composes == 1 and desktop.submits == 0
    restarted = bridge(tmp_path, desktop)
    assert prepare(restarted, token)["commit_token"] == queued["commit_token"]
    posted = commit(restarted, token, queued)
    assert posted["status"] == "posted" and posted["message_receipt"]["native_message_id"] == "new"
    assert "message" not in posted and "target_token" not in posted and "commit_token" not in posted
    assert posted["recipient_id"] == queued["recipient_id"] and posted["task_id"] == queued["task_id"]
    assert prepare(restarted, token)["status"] == "posted" and desktop.submits == 1


@pytest.mark.parametrize("mutation,code", [
    (lambda s: s["nodes"].append(dict(s["nodes"][1], id="other-editor")), "ambiguous_composer"),
    (lambda s: s["nodes"].append({"id": "approval", "role": "Button", "name": "Approve"}), "approval_surface_active"),
    (lambda s: s["nodes"].__setitem__(3, {"id": "shell", "role": "Text", "name": "PS>"}), "unverified_conversation"),
    (lambda s: s["nodes"][1].update(parent="another-pane"), "unverified_task_pane"),
])
def test_ambiguous_or_wrong_surface_has_no_control(mutation, code):
    value = snapshot()
    mutation(value)
    with pytest.raises(RecipientError, match=code):
        recipient_view(value)


def test_title_does_not_authorize_another_window_or_pane(tmp_path):
    desktop = Desktop()
    service = bridge(tmp_path, desktop)
    token = selected(service)
    desktop.state["window"]["pid"] += 1
    with pytest.raises(RecipientError, match="recipient_window_changed"):
        prepare(service, token)
    assert desktop.composes == desktop.submits == 0


@pytest.mark.parametrize("change", ["text", "pane", "approval"])
def test_current_recipient_and_text_reverified_before_submission(tmp_path, change):
    desktop = Desktop()
    service = bridge(tmp_path, desktop)
    token = selected(service)
    queued = prepare(service, token)
    if change == "text":
        desktop.state["nodes"][1]["value"] = "operator draft"
    elif change == "pane":
        desktop.state["nodes"][0]["id"] = "different-pane"
    else:
        desktop.state["nodes"].append({"id": "approval", "role": "Button", "name": "Approve"})
    with pytest.raises(RecipientError):
        commit(service, token, queued)
    assert desktop.submits == 0


def test_lost_prepare_response_recovers_without_typing_twice(tmp_path):
    desktop = Desktop()
    desktop.lose_prepare = True
    service = bridge(tmp_path, desktop)
    token = selected(service)
    queued = prepare(service, token)
    assert queued["status"] == "queued" and desktop.composes == 1
    assert service.reconcile(operation_id="operation", target_token=token)["commit_token"] == queued["commit_token"]
    assert desktop.composes == 1 and desktop.submits == 0


@pytest.mark.parametrize("posted", [False, True])
def test_uncertain_submit_only_reconciles_never_reinvokes(tmp_path, posted):
    desktop = Desktop()
    desktop.lose_submit, desktop.no_post = True, not posted
    service = bridge(tmp_path, desktop)
    token = selected(service)
    queued = prepare(service, token)
    receipt = commit(service, token, queued)
    assert receipt["status"] == ("posted" if posted else "unknown")
    assert prepare(service, token)["status"] == receipt["status"]
    assert desktop.submits == 1


def test_old_exact_message_is_not_a_new_post_receipt(tmp_path):
    desktop = Desktop()
    desktop.state["nodes"][4]["name"] = "requested message"
    desktop.no_post = True
    service = bridge(tmp_path, desktop)
    token = selected(service)
    receipt = commit(service, token, prepare(service, token))
    assert receipt["status"] == "unknown" and desktop.submits == 1


def test_scope_conflicts_and_reauthorization(tmp_path):
    desktop = Desktop()
    allowed = [True]
    calls = []
    def authorize():
        calls.append(True)
        if not allowed[0]:
            raise RecipientError("revoked", 403)
    service = bridge(tmp_path, desktop, authorize=authorize)
    token = selected(service)
    queued = prepare(service, token)
    with pytest.raises(RecipientError, match="recipient_not_found"):
        bridge(tmp_path, desktop, owner="other").select(token)
    with pytest.raises(RecipientError, match="operation_conflict"):
        prepare(service, token, "different message")
    allowed[0] = False
    with pytest.raises(RecipientError, match="revoked"):
        commit(service, token, queued)
    assert desktop.submits == 0 and len(calls) >= 4


def test_capture_is_on_demand_and_has_real_artifact_receipt(tmp_path):
    desktop = Desktop()
    service = bridge(tmp_path, desktop)
    token = selected(service)
    inspected = service.inspect(target_token=token)
    assert "artifact" not in inspected and desktop.captures == 0
    captured = service.inspect(target_token=token, capture=True)
    from pathlib import Path
    artifact = captured["artifact"]
    assert captured["status"] == "completed" and artifact["artifact_id"] == captured["artifact_id"]
    assert artifact["window"]["pid"] == WINDOW["pid"]
    assert Path(artifact["path"]).read_bytes().startswith(b"\x89PNG")
    assert captured["captured_at"] == artifact["captured_at"] and desktop.captures == 1


def test_capture_refuses_changed_window_before_receipt(tmp_path):
    desktop = Desktop()
    original = desktop.capture
    def capture(target):
        value = original(target)
        desktop.state["window"]["process_started"] = "reused-pid"
        return value
    desktop.capture = capture
    service = bridge(tmp_path, desktop)
    token = selected(service)
    with pytest.raises(RecipientError, match="recipient_window_changed"):
        service.inspect(target_token=token, capture=True)
    assert not list(tmp_path.rglob("*.png"))


def test_duplicate_concurrent_operation_only_prepares_once(tmp_path):
    desktop = Desktop()
    first = bridge(tmp_path, desktop)
    token = selected(first)
    results = []
    workers = [threading.Thread(target=lambda: results.append(prepare(bridge(tmp_path, desktop), token)))
               for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
    assert len(results) == 2 and results[0]["commit_token"] == results[1]["commit_token"]
    assert desktop.composes == 1 and desktop.submits == 0


def test_replaced_old_accessibility_ids_are_not_post_receipts(tmp_path):
    desktop = Desktop()
    desktop.no_post = True
    desktop.state["nodes"][4]["name"] = "requested message"
    service = bridge(tmp_path, desktop)
    token = selected(service)
    queued = prepare(service, token)
    desktop.state["nodes"][3]["id"] = "replaced-old"
    assert commit(service, token, queued)["status"] == "unknown"


def test_native_pre_action_guard_revocation_is_not_swallowed(tmp_path):
    desktop = Desktop()
    service = bridge(tmp_path, desktop)
    token = selected(service)
    queued = prepare(service, token)
    original = desktop.submit
    def submit(target, message, submit_id, *, authorize):
        service.authorize = lambda: (_ for _ in ()).throw(RecipientError("revoked", 403))
        return original(target, message, submit_id, authorize=service.authorize)
    desktop.submit = submit
    with pytest.raises(RecipientError, match="revoked"):
        commit(service, token, queued)
    assert desktop.submits == 0
