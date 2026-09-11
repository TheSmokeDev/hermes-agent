"""Current-turn steering for the stock in-process loop; reuses its existing queue."""
from __future__ import annotations

from agent.interrupt_control import InterruptControlMixin, _ic_lock


def steering_target(agent):
    target = {"session_id": str(getattr(agent, "session_id", "") or ""),
              "turn_id": str(getattr(agent, "_current_turn_id", "") or "")}
    supported = (
        getattr(getattr(agent, "steer", None), "__func__", None) is InterruptControlMixin.steer
        and getattr(agent, "api_mode", None) in {
            "chat_completions", "codex_responses", "anthropic_messages", "bedrock_converse"}
        and not str(getattr(agent, "base_url", "")).startswith("acp://"))
    active = getattr(agent, "_model_request_active", None)
    active = bool((active is not None and active.is_set()) or getattr(agent, "_executing_tools", False))
    reason = "backend_unsupported" if not supported else "turn_not_active"
    accepting = supported and active and bool(target["turn_id"]) and not getattr(agent, "_interrupt_requested", False)
    return {"supported": accepting, "reason": None if accepting else reason, **target}


def steer_current_turn(agent, text, *, expected_session_id, expected_turn_id):
    # Same cancellation lock as interrupt/redirect. No cancellation, wakeup or replacement run.
    with _ic_lock(agent, "_pending_redirect_lock"):
        target = steering_target(agent)
        if not target["supported"]:
            return "unsupported" if target["reason"] == "backend_unsupported" else "rejected"
        if target["session_id"] != expected_session_id or target["turn_id"] != expected_turn_id:
            return "rejected"
        return "queued" if agent.steer(text) else "rejected"


def bind_origin_steer(agent, text, origin, *, expected_session_id, expected_turn_id):
    """Mint a trusted row-bound queue value using only this run's host-owned store and lease."""
    from agent.steer_origin import bound_steer_text
    from passive_history_ingress import PRODUCER
    def current():
        target = steering_target(agent)
        return (target["supported"] and target["session_id"] == expected_session_id
                and target["turn_id"] == expected_turn_id)
    with _ic_lock(agent, "_pending_redirect_lock"):
        if not current():
            raise ValueError("Steering target no longer accepts origin binding")
        result = agent._session_db.bind_ordinary_steer_origin(expected_session_id,
            producer=PRODUCER, content=text, **origin, target_guard=current,
            turn_lease_holder=getattr(agent, "_active_session_turn_lease_holder", None))
        return bound_steer_text(text, result["message"]), result
