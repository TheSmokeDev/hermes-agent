"""Linked child mode for existing API runs; parent materialization never calls its model."""
from __future__ import annotations

from agent.subagent_lifecycle import (
    SubagentLaunchRequest, SubagentLifecycleService, SubagentState,
)
from hermes_state_passive_history import _validated_identifier


def capabilities():
    from agent.task_worker_registry import available_workers
    return {"version": 1, "supported": True, "origin_sources": ["fresh", "passive_receipt"],
            "max_goal_chars": 16000, "max_context_chars": 32000,
            "separate_child_goal": True, "owning_run_approvals": True, "restart_relaunch": False,
            "external_workers": {"version": 1, "names": available_workers(),
                                 "discord_task_context": {"version": 1, "event_proof_required": True}}}


def validate_child_request(child):
    required = {"goal", "correlation_id"}
    optional = {"context", "allowed_toolsets", "worker", "attachments"}
    if not isinstance(child, dict) or not required <= set(child) or set(child) - required - optional:
        raise ValueError("child requires goal/correlation_id and supported optional fields only")
    if not isinstance(child["goal"], str) or not child["goal"].strip() or len(child["goal"]) > 16000:
        raise ValueError("child goal must be nonempty and at most 16000 characters")
    _validated_identifier(child["correlation_id"], "correlation_id", 128)
    context = child.get("context")
    if context is not None and (not isinstance(context, str) or len(context) > 32000):
        raise ValueError("child context must be at most 32000 characters")
    if "attachments" in child:
        from gateway.platforms.api_server_input_attachments import delivery_refusal
        from hermes_state_input_attachments import normalize_references
        # Shape/duplicate rejection is the store's own contract; reuse it rather than restating it.
        normalize_references(child["attachments"])
        refusal = delivery_refusal(child)
        if refusal is not None:
            raise ValueError("Attachment delivery is unavailable for this child: " + refusal)
    if "worker" in child:
        from agent.task_worker_registry import configured_worker
        _validated_identifier(child["worker"], "worker", 128)
        if "allowed_toolsets" in child or configured_worker(child["worker"]) is None:
            raise ValueError("External worker is unavailable or has incompatible toolset overrides")
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
    from gateway.platforms.api_server_task_workers import current_worker
    worker = current_worker(parent, approval_key)
    if worker is not None:
        worker.cancel()
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
    from hermes_state_input_attachments import audience_key
    from gateway.platforms.api_server_input_attachments import manifest_context
    # Revalidates immutable bytes, ownership and the current Discord audience before launch;
    # the frozen set must match this request's references exactly.
    verified = db.claim_child_dispatch(
        dispatch, attachments=request.get("attachments"),
        attachment_audience=audience_key(getattr(run, "discord_task_context", None)))
    if "worker" in request:
        if verified:
            raise ValueError("Installed task workers expose no host-file delivery contract")
        from gateway.platforms.api_server_task_workers import run_worker_sync
        return run_worker_sync(adapter, run, parent)
    service = SubagentLifecycleService(lambda: parent)
    handle = None
    prior_turn = getattr(parent, "_current_turn_id", None)
    parent._current_turn_id = run.run_id
    try:
        handle = service.launch(SubagentLaunchRequest(
            goal=request["goal"], context=manifest_context(request.get("context"), verified),
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
