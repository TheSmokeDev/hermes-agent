"""Public child steering controls the capability-owned live child only."""
from dataclasses import replace
import threading
from types import SimpleNamespace

from agent.run_steering import steer_current_turn, steering_target
from agent.subagent_lifecycle import SubagentLaunchRequest, SubagentLifecycleService
from tests.run_agent.test_steer import _bare_agent


def test_public_child_steer_fences_owner_turn_and_cancellation(monkeypatch):
    parent = SimpleNamespace(session_id="parent", enabled_toolsets=["file"])
    child = _bare_agent()
    child.session_id, child._current_turn_id = "child-session", "child:turn"
    child._subagent_id, child.provider, child.model = "child-handle", "fixture", "fixture"
    ready, finish = threading.Event(), threading.Event()
    def run(*args):
        child._model_request_active.set()
        ready.set()
        assert finish.wait(10)
        return {"status": "interrupted" if child._interrupt_requested else "completed"}
    monkeypatch.setattr("tools.delegate_tool._build_child_preserving_parent_tools", lambda **kw: child)
    monkeypatch.setattr("tools.delegate_tool._run_child_lifecycle", run)
    service = SubagentLifecycleService(lambda: parent)
    handle = service.launch(SubagentLaunchRequest(goal="child goal"))
    try:
        assert ready.wait(5)
        assert service.steering(handle)["session_id"] == "child-session"
        kw = {"expected_session_id": "child-session", "expected_turn_id": "child:turn"}
        assert service.steer(replace(handle, capability="forged"), "no", **kw) == "rejected"
        foreign = SubagentLifecycleService(lambda: SimpleNamespace(session_id="foreign"))
        assert foreign.steer(handle, "no", **kw) == "rejected"
        assert service.steer(handle, "no", **{**kw, "expected_turn_id": "old"}) == "rejected"
        assert service.steer(handle, "correct course", **kw) == "queued"
        assert child._pending_steer == "correct course"
        assert service.cancel(handle, reason="explicit stop").accepted
        assert service.steer(handle, "late", **kw) == "rejected"
        assert not service.steering(handle)["supported"]
    finally:
        finish.set()
        service.wait(handle, timeout_seconds=5)
    assert service.steer(handle, "terminal", **kw) == "rejected"


def test_native_or_inactive_runtime_never_claims_queue_support():
    agent = _bare_agent()
    agent.session_id, agent._current_turn_id = "session", "turn"
    kw = {"expected_session_id": "session", "expected_turn_id": "turn"}
    assert not steering_target(agent)["supported"]
    assert steer_current_turn(agent, "late", **kw) == "rejected"
    agent._model_request_active.set()
    agent.api_mode = "codex_app_server"
    assert steer_current_turn(agent, "native", **kw) == "unsupported"
    agent.api_mode, agent.base_url = "chat_completions", "acp://fixture"
    assert steer_current_turn(agent, "detached", **kw) == "unsupported"
    assert agent._pending_steer is None and not agent._interrupt_requested
