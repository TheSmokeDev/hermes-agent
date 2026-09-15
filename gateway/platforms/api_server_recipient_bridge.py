"""Configured-peer API for explicit, verified native recipient operations."""
from __future__ import annotations

import asyncio

from aiohttp import web

from gateway.discord_task_context import DiscordTaskContextError
from gateway.platforms.api_server_discord_context import verify_request
from tools.computer_use.recipient_bridge import RecipientBridge
from tools.computer_use.recipient_contract import RecipientError, digest, identifier

OPERATIONS = {
    "probe": set(), "list": {"app"}, "select": {"target_token"},
    "send": {"operation_id", "target_token", "message", "commit_token"},
    "reconcile": {"operation_id", "target_token"}, "inspect": {"target_token", "capture"},
    "catalog": {"app", "limit", "cursor"}, "history": {"target_token", "limit", "cursor"},
    "status": {"target_token"},
}


def authority(adapter, request):
    if not adapter._expected_api_key() or adapter._room_grant_token(request) or adapter._check_auth(request) is not None:
        raise RecipientError("recipient_auth_required", 401)
    from gateway.platforms.api_server import _api_request_profile
    profile = _api_request_profile.get() or getattr(adapter.gateway_runner, "_primary_profile_name", None) or "default"
    return profile, adapter._run_idempotency_scope(request)


async def handle(adapter, request, *, action):
    try:
        profile, peer = authority(adapter, request)
        try:
            body = await request.json()
        except Exception:
            raise RecipientError("invalid_recipient_request", 400) from None
        common = {"session_id", "actor_scope", "discord_binding"}
        if not isinstance(body, dict) or set(body) - common - OPERATIONS[action]:
            raise RecipientError("invalid_recipient_request", 400)
        session_id = identifier(body.get("session_id"), "session_id")
        actor_scope = identifier(body.get("actor_scope"), "actor_scope")
        db = await adapter._ensure_session_db_async()
        if db is None or db.get_session(session_id) is None:
            raise RecipientError("recipient_session_not_found", 404)

        def authorize():
            current_profile, current_peer = authority(adapter, request)
            if (current_profile, current_peer) != (profile, peer):
                raise RecipientError("recipient_authority_changed", 403)
            if db.get_session(session_id) is None:
                raise RecipientError("recipient_session_not_found", 404)
            verify_request(adapter, request, session_id=session_id, binding=body.get("discord_binding"))

        authorize()
        bridge = RecipientBridge(session_id=session_id, owner=digest([profile, peer, actor_scope]),
                                 authorize=authorize)
        arguments = {key: value for key, value in body.items() if key in OPERATIONS[action]}
        required = OPERATIONS[action] - {"app", "commit_token", "capture", "limit", "cursor"}
        if required - arguments.keys():
            raise RecipientError("invalid_recipient_request", 400)
        method = bridge.list_recipients if action == "list" else getattr(bridge, action)
        result = await asyncio.to_thread(method, **arguments)
        authorize()
        return web.json_response(result, headers={"Cache-Control": "no-store"})
    except (RecipientError, DiscordTaskContextError) as exc:
        return web.json_response({"error": exc.code}, status=exc.status, headers={"Cache-Control": "no-store"})


def http_routes(adapter):
    def route(action):
        async def handler(request):
            return await handle(adapter, request, action=action)
        return handler
    return [("POST", "/v1/recipient-bridge/" + action, route(action)) for action in OPERATIONS]
