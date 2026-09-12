"""Opt-in durable steering receipts on the existing owning-run control subresource."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from aiohttp import web

from hermes_state_passive_history import PassiveHistoryBusyError, PassiveHistoryRetiredError, _validated_identifier
from hermes_state_errors import SessionTurnLeaseLostError


def capabilities():
    return {"version": 1, "supported": True, "durable_actions": True,
            "receipt_states": ["queued", "rejected", "unsupported", "unknown"],
            "origin_sources": {"linked_child": ["passive_receipt"], "ordinary": ["passive_receipt", "pending"]},
            "max_input_chars": 16000}


def live_target(adapter, run_id, agent, status):
    from agent.run_steering import steering_target
    if run_id in adapter._stopping_run_ids or status.get("status") not in {"running", "waiting_for_approval"}:
        return {"supported": False, "reason": "run_not_accepting_steer"}, None
    from gateway.platforms.api_server_task_workers import current_worker
    worker = current_worker(agent, run_id)
    if worker is not None:
        return worker.steering(), worker
    control = getattr(agent, "_api_linked_child_control", None)
    linked = "child_correlation_id" in status or "child_id" in status or control is not None
    if linked:
        if control is None or control[2] != adapter._run_approval_sessions.get(run_id):
            return {"supported": False, "reason": "child_not_available"}, None
        return {**control[0].steering(control[1]), "kind": "linked_child"}, control
    return {**steering_target(agent), "kind": "ordinary"}, None


def _validate(body):
    if not isinstance(body, dict) or set(body) != {"input", "control"}:
        raise ValueError("Expected input and control")
    text, control = body["input"], body["control"]
    if not isinstance(text, str) or not text.strip() or len(text) > 16000:
        raise ValueError("Invalid correction text")
    required = {"version", "action_id", "expected_session_id", "expected_turn_id"}
    if not isinstance(control, dict) or not required <= set(control) or set(control) - required - {"origin"}:
        raise ValueError("Invalid control fields")
    if type(control["version"]) is not int or control["version"] != 1:
        raise ValueError("Unsupported control version")
    _validated_identifier(control["action_id"], "action_id", 256)
    # Host-issued turn IDs contain colons; they are opaque, not ingress event identifiers.
    for key in ("expected_session_id", "expected_turn_id"):
        value = control[key]
        if not isinstance(value, str) or not 1 <= len(value) <= 512 or any(ord(char) < 32 for char in value):
            raise ValueError("Invalid host target identity")
    origin = control.get("origin")
    if origin is not None:
        if not isinstance(origin, dict) or set(origin) not in (
            {"event_id", "origin_turn_id"}, {"event_id", "origin_turn_id", "receipt_id"}):
            raise ValueError("Invalid origin")
        for key in ("event_id", "origin_turn_id"):
            _validated_identifier(origin[key], key, 128)
        if "receipt_id" in origin and (type(origin["receipt_id"]) is not int or origin["receipt_id"] < 1):
            raise ValueError("Invalid origin receipt")
    return text, control


async def handle(adapter, request, *, _api_server, body=None):
    from gateway.platforms.api_server_runs import _load_owned_run, _mark_run_event
    if not adapter._expected_api_key():
        return adapter._auth_failed_response()
    run_id, status, agent, _, error = _load_owned_run(
        adapter, request, _api_server=_api_server, permission=None, active_fallback=False)
    if error is not None:
        return error
    store, scope = adapter._run_idempotency_store, adapter._run_idempotency_scope(request)
    headers = {"Cache-Control": "no-store"}
    def response(value, status=200):
        return web.json_response(value, status=status, headers=headers)
    try:
        if request.method == "GET":
            action_id = request.query.get("action_id")
            if action_id is None:
                target, _ = live_target(adapter, run_id, agent, status)
                if not store.durable or not store.owns_run(scope, run_id):
                    target = {"supported": False, "reason": "durable_run_required"}
                return response({"object": "hermes.run.steering", "version": 1, "run_id": run_id,
                                 "status": status["status"], **target})
            _validated_identifier(action_id, "action_id", 256)
            _, receipt = store.steer_receipt(scope, run_id, action_id)
            return response(receipt if receipt else {"error": "steer_receipt_not_found"}, 200 if receipt else 404)
        text, control = _validate(body)
        if not store.durable or not store.owns_run(scope, run_id):
            return response({"error": "durable_run_required", "status": "unsupported"}, 409)
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        action_id = control["action_id"]
        outcome, prior = store.steer_receipt(scope, run_id, action_id, fingerprint=fingerprint)
        if outcome == "conflict":
            return response({"error": "steer_action_conflict"}, 409)
        if prior is not None:
            return response(prior)
        # No await after this fresh status/target check and before queue submission. Stop/auth state
        # cannot change on this event loop while the action is admitted. The backend checks its turn.
        current = adapter._run_statuses.get(run_id, status)
        target, child = live_target(adapter, run_id, agent, current)
        receipt = {"object": "hermes.run.steer", "version": 1, "run_id": run_id, "action_id": action_id,
                   "status": "unknown", "evidence": "reserved_before_queue", "created_at": time.time(),
                   "session_id": control["expected_session_id"], "turn_id": control["expected_turn_id"]}
        origin = control.get("origin")
        if origin is not None:
            receipt["origin"] = origin
        reason = None
        if not target["supported"]:
            reason = target["reason"]
            receipt["status"] = "unsupported" if reason == "backend_unsupported" else "rejected"
        elif target["session_id"] != control["expected_session_id"] or target["turn_id"] != control["expected_turn_id"]:
            receipt["status"], reason = "rejected", "stale_target"
        if reason:
            receipt["evidence"] = reason
        # Origin binding may persist pending input. Reserve the immutable action before
        # that first side effect so a competing request cannot write a losing origin.
        outcome, stored = store.steer_receipt(scope, run_id, action_id, fingerprint=fingerprint, reserve=receipt)
        if outcome != "created":
            return response({"error": "steer_action_conflict"}, 409) if outcome == "conflict" else response(stored)
        if reason:
            return response(receipt)
        if origin is not None:
            try:
                if child is None:
                    from agent.run_steering import bind_origin_steer
                    text, binding = bind_origin_steer(agent, text, origin,
                        expected_session_id=control["expected_session_id"], expected_turn_id=control["expected_turn_id"])
                    receipt["parent_message_id"] = binding["message"]["_row_id"]
                    receipt["origin"] = {**origin, "receipt_id": binding["receipt_id"]}
                else:
                    if "receipt_id" not in origin:
                        raise ValueError("Child steering requires an existing origin receipt")
                    receipt["parent_message_id"] = agent._session_db.verify_child_steer_origin(
                        run_id, run_scope=scope, child_id=target["child_id"], content=text, **origin)
            except (ValueError, PassiveHistoryBusyError, PassiveHistoryRetiredError, SessionTurnLeaseLostError):
                receipt["status"], reason = "rejected", "origin_unavailable"
        if reason:
            receipt["evidence"] = reason
            store.settle_steer_receipt(scope, run_id, action_id, receipt)
            return response(receipt)
        try:
            kw = {"expected_session_id": control["expected_session_id"], "expected_turn_id": control["expected_turn_id"]}
            from gateway.platforms.api_server_task_workers import WorkerBinding
            if isinstance(child, WorkerBinding):
                state = child.steer(text, action_id=action_id, **kw)
            elif child is not None:
                state = child[0].steer(child[1], text, **kw)
            else:
                from agent.run_steering import steer_current_turn
                state = steer_current_turn(agent, text, **kw)
        except Exception:
            # Persisted unknown is a deliberate no-replay boundary: backend acceptance may have happened.
            return response(store.steer_receipt(scope, run_id, action_id)[1])
        receipt.update(status=state, evidence="backend_queue_ack" if state == "queued" else "backend_refused")
        store.settle_steer_receipt(scope, run_id, action_id, receipt)
        if state == "queued":
            _mark_run_event(adapter, run_id, "run.steer_queued", action_id=action_id)
        return response(receipt)
    except (ValueError, TypeError, UnicodeError):
        return response({"error": "invalid_steer_control"}, 400)
    except (sqlite3.Error, RuntimeError):
        return response({"error": "steer_store_unavailable", "retryable": False}, 503)
