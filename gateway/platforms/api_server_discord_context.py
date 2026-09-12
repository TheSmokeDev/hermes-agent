"""Configured-peer transport for event-issued Discord task context proofs."""
from __future__ import annotations

from aiohttp import web

from gateway.discord_task_context import DiscordTaskContextError, _identifier, task_contexts

PROOF_HEADER = "X-Hermes-Discord-Task-Proof"
FIELDS = {"proof", "session_id", "binding_id"}


def _authority(adapter, request):
    if not adapter._expected_api_key() or adapter._room_grant_token(request):
        raise DiscordTaskContextError("discord_context_auth_required", 401)
    error = adapter._check_auth(request)
    if error is not None:
        raise DiscordTaskContextError("discord_context_auth_required", 401)
    from gateway.platforms.api_server import _api_request_profile
    profile = _api_request_profile.get() or getattr(adapter.gateway_runner, "_primary_profile_name", None) or "default"
    return task_contexts(adapter.gateway_runner), profile, adapter._run_idempotency_scope(request)


def error_response(exc):
    return web.json_response({"error": exc.code}, status=exc.status, headers={"Cache-Control": "no-store"})


def verify_request(adapter, request, *, session_id, binding=None):
    """Explicit room claims are checked; ordinary canonical API access is unchanged."""
    if binding is None:
        proof = request.headers.get(PROOF_HEADER)
        if proof is None:
            return None
        binding = {"proof": proof, "session_id": session_id}
    else:
        if not isinstance(binding, dict) or set(binding) != FIELDS or binding["session_id"] != session_id:
            raise DiscordTaskContextError("invalid_discord_binding", 400)
    for value in binding.values():
        _identifier(value)
    contexts, profile, owner = _authority(adapter, request)
    return contexts.verify(**binding, profile=profile, owner=owner)


async def handle(adapter, request, *, action):
    try:
        contexts, profile, owner = _authority(adapter, request)
        try:
            body = await request.json()
        except Exception:
            raise DiscordTaskContextError("invalid_discord_binding", 400) from None
        expected = FIELDS | ({"next_session_id", "next_binding_id"} if action == "rebind" else set())
        if not isinstance(body, dict) or set(body) != expected:
            raise DiscordTaskContextError("invalid_discord_binding", 400)
        for value in body.values():
            _identifier(value)
        if action == "revoke":
            contexts.revoke(**body, profile=profile, owner=owner)
            result = {"revoked": True}
        else:
            db = await adapter._ensure_session_db_async()
            selected = body.get("next_session_id", body["session_id"])
            if not isinstance(selected, str) or db is None or db.get_session(selected) is None:
                raise DiscordTaskContextError("discord_session_not_found", 404)
            if action == "rebind":
                result = contexts.rebind(**body, profile=profile, owner=owner)
            else:
                result = contexts.verify(**body, profile=profile, owner=owner, redeem=action == "redeem")
        return web.json_response(result, headers={"Cache-Control": "no-store"})
    except DiscordTaskContextError as exc:
        return error_response(exc)


def http_routes(adapter):
    def route(action):
        async def handler(request):
            return await handle(adapter, request, action=action)
        return handler
    return [("POST", "/v1/task-context/discord/" + action, route(action))
            for action in ("redeem", "verify", "rebind", "revoke")]
