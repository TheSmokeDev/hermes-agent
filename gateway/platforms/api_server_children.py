"""Linked child mode for existing API runs; parent materialization never calls its model."""
from __future__ import annotations

from agent.subagent_lifecycle import (
    SubagentLaunchRequest, SubagentLifecycleService, SubagentState,
)
from hermes_state_passive_history import _validated_identifier


def capabilities():
    return {"version": 1, "supported": True, "origin_sources": ["fresh", "passive_receipt"],
            "max_goal_chars": 16000, "max_context_chars": 32000,
            "separate_child_goal": True, "owning_run_approvals": True, "restart_relaunch": False}


def validate_child_request(child):
    required, optional = {"goal", "correlation_id"}, {"context", "allowed_toolsets"}
    if not isinstance(child, dict) or not required <= set(child) or set(child) - required - optional:
        raise ValueError("child requires goal/correlation_id and supported optional fields only")
    if not isinstance(child["goal"], str) or not child["goal"].strip() or len(child["goal"]) > 16000:
        raise ValueError("child goal must be nonempty and at most 16000 characters")
    _validated_identifier(child["correlation_id"], "correlation_id", 128)
    context = child.get("context")
    if context is not None and (not isinstance(context, str) or len(context) > 32000):
        raise ValueError("child context must be at most 32000 characters")
    requested = child.get("allowed_toolsets")
    if requested is not None:
        from toolsets import TOOLSETS
        if not isinstance(requested, list) or not 1 <= len(requested) <= 32 or any(
            not isinstance(name, str) or name not in TOOLSETS for name in requested
        ):
            raise ValueError("allowed_toolsets must be a nonempty bounded list of known toolsets")


def cancel_linked_child(parent, *, approval_key):
    # The child is live as soon as launch submits it, before the handle is published here.
    # Revoke the owning run's transport first, even during that publication gap.
    from tools.approval import unregister_gateway_notify
    if approval_key:
        unregister_gateway_notify(approval_key)
    control = getattr(parent, "_api_linked_child_control", None)
    if control is not None and control[2] == approval_key:
        service, handle, _ = control
        service.cancel(handle, reason="Owning API run stopped")


def run_child_sync(adapter, run, parent):
    """Called inside the existing run's profile/session/approval scope; hold it until terminal."""
    dispatch, request = run.child_dispatch, run.child_request
    if run.run_id in adapter._stopping_run_ids:
        return {"interrupted": True, "final_response": "Stopped before child dispatch"}
    db = getattr(parent, "_session_db", None)
    if db is None or str(getattr(parent, "session_id", "")) != dispatch["parent_session_id"]:
        raise ValueError("Linked child dispatch requires the authorized parent and its durable store")
    db.claim_child_dispatch(dispatch)
    service = SubagentLifecycleService(lambda: parent)
    handle = None
    prior_turn = getattr(parent, "_current_turn_id", None)
    parent._current_turn_id = run.run_id
    try:
        handle = service.launch(SubagentLaunchRequest(
            goal=request["goal"], context=request.get("context"),
            allowed_toolsets=tuple(request["allowed_toolsets"]) if request.get("allowed_toolsets") else None,
            parent_session_id=dispatch["parent_session_id"], correlation_id=run.run_id,
            metadata={"run_id": run.run_id, "event_id": dispatch["event_id"],
                      "origin_turn_id": dispatch["origin_turn_id"], "correlation_id": request["correlation_id"],
                      "parent_message_id": dispatch["parent_message_id"]}))
        parent._api_linked_child_control = (service, handle, run.approval_session_key)
        if not handle.child_session_id:
            raise ValueError("Host lifecycle does not expose linked child session identity")
        db.record_child_dispatch_handle(dispatch, child_id=handle.subagent_id,
                                        child_session_id=handle.child_session_id)
        current = adapter._run_statuses.get(run.run_id, {})
        phase = current.get("status", "running")
        adapter._set_run_status(run.run_id, phase, child_id=handle.subagent_id,
                                child_session_id=handle.child_session_id,
                                parent_message_id=dispatch["parent_message_id"],
                                last_event=current.get("last_event") if phase != "running" else "child.started")
        if run.run_id in adapter._stopping_run_ids:
            cancel_linked_child(parent, approval_key=run.approval_session_key)
        service.wait(handle)
        result = service.result(handle)
        interrupted = result.terminal_state in {SubagentState.INTERRUPTED, SubagentState.CANCELLED}
        return {"final_response": result.summary or result.error_message or "",
                "failed": result.terminal_state is not SubagentState.SUCCEEDED,
                "interrupted": interrupted, "error": result.error_message,
                "child_id": handle.subagent_id, "child_session_id": handle.child_session_id}
    except BaseException:
        if handle is not None:
            from tools.approval import unregister_gateway_notify
            unregister_gateway_notify(run.approval_session_key)
            service.cancel(handle, reason="Owning run could not retain child dispatch")
            service.wait(handle)
        raise
    finally:
        parent._current_turn_id = prior_turn
        parent._api_linked_child_control = None
