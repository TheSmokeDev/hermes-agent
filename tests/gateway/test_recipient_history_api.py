"""Authenticated route adapters read real bounded native files without dispatch."""
import json

import pytest

from gateway.platforms import api_server_recipient_bridge as api
from tools.computer_use import recipient_history_sources as sources
from tools.computer_use.recipient_bridge import RecipientBridge
from tests.gateway.test_dashboard_consumption import gateway
from tests.gateway.test_discord_task_context import binding, room_fixture
from tests.tools.test_recipient_history import native_sources as native_sources, TASKS


@pytest.mark.asyncio
@pytest.mark.parametrize("app", ["codex_desktop", "claude_code"])
async def test_history_routes_preserve_profile_actor_peer_and_selected_identity(tmp_path, monkeypatch, native_sources, app):
    env = native_sources
    monkeypatch.setattr(api, "RecipientBridge", lambda **kw: RecipientBridge(
        **kw, desktop=env.desktop, claude=env.claude))
    async with gateway(tmp_path, monkeypatch) as (_, keys, stores, adapter, client):
        auth = {"Authorization": "Bearer " + keys["alpha"]}
        base = "/p/alpha/v1/recipient-bridge/"
        body = {"session_id": "same-session", "actor_scope": "operator"}
        response = await client.post(base + "catalog", headers=auth, json={**body, "app": app, "limit": 1})
        assert response.status == 200, await response.text()
        assert response.headers["Cache-Control"] == "no-store"
        catalog = await response.json()
        target = catalog["recipients"][0]
        arguments = {**body, "target_token": target["target_token"]}
        for operation in ("select", "history", "status"):
            response = await client.post(base + operation, headers=auth, json=arguments)
            assert response.status == 200, await response.text()
            result = await response.json()
            assert all(result[k] == target[k] for k in ("recipient_id", "app", "task_id", "host_id"))
            assert str(env.paths[app, TASKS[0]]) not in json.dumps(result)
            if operation == "history":
                assert result["messages"][1]["text"] == "answer " + TASKS[0]
            if operation == "status":
                assert result["status"] == "unknown" and not result["completion_tracking"]
            assert (await client.post(base + operation, json=arguments)).status == 401
            assert (await client.post(base + operation, headers=auth,
                                       json={**arguments, "actor_scope": "foreign"})).status == 404
            assert (await client.post(base.replace("alpha", "beta") + operation,
                                       headers={"Authorization": "Bearer " + keys["beta"]}, json=arguments)).status == 404
        for extra in ({"path": str(env.paths[app, TASKS[0]])}, {"capture": True}, {"limit": True}, {"limit": 51}):
            assert (await client.post(base + "history", headers=auth, json={**arguments, **extra})).status == 400
        assert (await client.post(base + "history", headers=auth, json=body)).status == 400
        assert (await client.post(base + "catalog", headers=auth, json={**body, "app": []})).status == 400
        second = await client.post(base + "catalog", headers=auth,
                                   json={**body, "app": app, "cursor": catalog["next_cursor"]})
        assert (await second.json())["recipients"][0]["task_id"] == TASKS[1]
        assert (await client.post(base + "send", headers=auth,
                                   json={**arguments, "operation_id": "no-send", "message": "forbidden"})).status == 409
        descriptor = await client.get("/p/alpha/v1/capabilities", headers=auth)
        capability = (await descriptor.json())["features"]["recipient_bridge"]
        assert all(op in capability["operations"] for op in ("catalog", "history", "status", "send"))
        assert capability["history"]["max_limit"] >= capability["history"]["default_limit"]
        monkeypatch.setattr(adapter, "_expected_api_key", lambda: "replacement-peer-key")
        foreign = await client.post(base + "history", headers={"Authorization": "Bearer replacement-peer-key"}, json=arguments)
        assert foreign.status == 404
        assert stores["alpha"].get_messages("same-session") == [] and not adapter._active_run_tasks
    assert env.desktop.composes == env.desktop.submits == env.desktop.captures == 0
    assert env.native.writes == [] and env.native.secrets_read == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["catalog", "history", "status"])
@pytest.mark.parametrize("when", ["before", "during"])
async def test_current_discord_audience_is_verified_around_native_reads(
        tmp_path, monkeypatch, native_sources, operation, when):
    env, room = native_sources, room_fixture()
    monkeypatch.setattr(api, "RecipientBridge", lambda **kw: RecipientBridge(
        **kw, desktop=env.desktop, claude=env.claude))
    async with gateway(tmp_path, monkeypatch) as (_, keys, _, adapter, client):
        room.runner.config = adapter.gateway_runner.config
        adapter.gateway_runner = room.runner
        proof = binding(room)
        auth = {"Authorization": "Bearer " + keys["alpha"]}
        redeemed = await client.post("/p/alpha/v1/task-context/discord/redeem", headers=auth, json=proof)
        assert redeemed.status == 200
        body = {"session_id": "same-session", "actor_scope": "operator", "discord_binding": proof}
        base = "/p/alpha/v1/recipient-bridge/"
        catalog = await client.post(base + "catalog", headers=auth, json={**body, "app": "codex_desktop"})
        target = (await catalog.json())["recipients"][0]
        reads = []
        original = sources.read_file
        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            reads.append(True)
            room.add(3)
            return result
        monkeypatch.setattr(sources, "read_file", read)
        if when == "before":
            room.add(3)
        arguments = {"app": "codex_desktop"} if operation == "catalog" else {"target_token": target["target_token"]}
        response = await client.post(base + operation, headers=auth, json={**body, **arguments})
        assert response.status == 403, await response.text()
        assert set(await response.json()) == {"error"}
        assert bool(reads) == (when == "during")
        assert env.desktop.composes == env.desktop.submits == env.desktop.captures == 0
