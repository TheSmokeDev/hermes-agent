"""Generic installed-worker execution on the existing authenticated linked-child run path."""
from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

from agent.task_worker_provider import TaskWorkerRequest, TaskWorkerSession
from agent.task_worker_registry import configured_worker


class WorkerBinding:
    def __init__(self, session, run_id, child_id, authorize):
        self.session, self.run_id, self.child_id = session, run_id, child_id
        self.authorize = authorize
        self.active = True

    def steering(self):
        target = self.session.steering()
        if not self.active or not self.authorize() or not isinstance(target, dict) or target.get("supported") is not True:
            return {"supported": False, "reason": "worker_not_available"}
        return {**target, "session_id": self.child_id, "child_id": self.child_id,
                "kind": "linked_child"}

    def steer(self, text, **control):
        if not self.active or not self.authorize() or control["expected_session_id"] != self.child_id:
            return "rejected"
        state = self.session.steer(text, **control)
        return state if state in {"queued", "rejected", "unsupported", "unknown"} else "unknown"

    def approve(self, request_id, choice):
        if not self.active or not self.authorize():
            raise ValueError("Worker approval is no longer authorized")
        return self.session.approve(request_id, choice)

    def cancel(self):
        if self.active:
            self.session.cancel()


def current_worker(parent, run_id):
    binding = getattr(parent, "_api_task_worker", None)
    return binding if isinstance(binding, WorkerBinding) and binding.active and binding.run_id == run_id else None


def run_worker_sync(adapter, run, parent):
    from agent.periodic_scheduler import schedule

    dispatch, request = run.child_dispatch, run.child_request
    provider = configured_worker(request["worker"])
    if provider is None:
        raise ValueError("Configured task worker is unavailable")
    db = parent._session_db
    child_id = "worker-" + uuid.uuid4().hex
    db.create_session(child_id, source="subagent", parent_session_id=dispatch["parent_session_id"],
                      model_config={"task_worker": provider.name, "_delegate_from": dispatch["parent_session_id"]})
    holder = f"pid={os.getpid()}:turn={run.run_id}:platform=task_worker"
    if not db.acquire_session_turn_lease(child_id, holder, ttl_seconds=30, wait_seconds=0):
        raise ValueError("Task worker child is busy")
    binding, timer = None, None
    retired = threading.Event()

    def authorized():
        if retired.is_set():
            return False
        try:
            return db.child_dispatch_is_current(run.run_id, run_scope=dispatch["run_scope"],
                                                child_id=child_id, lease_holder=holder)
        except Exception:
            return False

    def report(data):
        if not isinstance(data, dict):
            raise ValueError("Invalid worker observation")
        phase = data.get("status", "running")
        if phase not in {"running", "waiting_for_approval"}:
            return
        if run.run_id not in adapter._run_streams or retired.is_set():
            return
        adapter._set_run_status(run.run_id, phase, last_event="worker.progress",
                               worker=provider.name, child_id=child_id, child_session_id=child_id)

    try:
        db.record_child_dispatch_handle(dispatch, child_id=child_id, child_session_id=child_id)
        db.append_message(child_id, "user", request["goal"], turn_lease_holder=holder,
                          display_kind="task_worker_goal",
                          display_metadata={"run_id": run.run_id, "origin_turn_id": dispatch["origin_turn_id"]})
        session = provider.open(TaskWorkerRequest(
            run_id=run.run_id, owner_scope=dispatch["run_scope"], profile=run.request_profile or "default",
            profile_home=Path(db.db_path).parent, parent_session_id=dispatch["parent_session_id"],
            child_session_id=child_id, action_id=request["correlation_id"],
            origin_turn_id=dispatch["origin_turn_id"], goal=request["goal"],
            context=request.get("context") or "", report=report, still_authorized=authorized))
        if not isinstance(session, TaskWorkerSession):
            raise ValueError("Task worker returned an invalid session")
        binding = WorkerBinding(session, run.run_id, child_id, authorized)
        parent._api_task_worker = binding

        def refresh():
            if (not authorized()
                or not db.refresh_session_turn_lease(child_id, holder, ttl_seconds=30)):
                retired.set()
                binding.cancel()

        timer = schedule(refresh, 10.0)
        report({"status": "running"})
        if run.run_id in adapter._stopping_run_ids:
            binding.cancel()
        result = session.run()
        if (not isinstance(result, dict) or result.get("status") not in {
            "completed", "failed", "cancelled", "unknown"
        } or not isinstance(result.get("output"), str)):
            raise ValueError("Task worker returned an invalid result")
        if retired.is_set():
            raise ValueError("Task worker owner was retired")
        if result["output"]:
            db.append_message(child_id, "assistant", result["output"], turn_lease_holder=holder,
                              display_kind="task_worker_result", display_metadata={"run_id": run.run_id})
        return {"final_response": result["output"], "artifacts": result.get("artifacts", []),
                "failed": result["status"] != "completed",
                "interrupted": result["status"] == "cancelled", "error": result.get("error"),
                "child_id": child_id, "child_session_id": child_id}
    finally:
        if binding is not None:
            binding.cancel()
            binding.active = False
        if timer is not None:
            timer.cancel(wait=1)
        if getattr(parent, "_api_task_worker", None) is binding:
            parent._api_task_worker = None
        db.release_session_turn_lease(child_id, holder)
