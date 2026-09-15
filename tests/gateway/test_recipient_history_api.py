"""Authenticated route adapters read real bounded native files without dispatch."""
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from gateway.platforms import api_server_recipient_bridge as api
from tools.computer_use import recipient_history
from tools.computer_use import recipient_history_sources as sources
from tools.computer_use.recipient_bridge import RecipientBridge
from tests.gateway.test_dashboard_consumption import gateway
from tests.gateway.test_discord_task_context import binding, room_fixture
from tests.tools.test_recipient_history import native_row, native_sources as native_sources, TASKS


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


@asynccontextmanager
async def routed(tmp_path, monkeypatch, env):
    """The real recipient-bridge routes over the real native fixture files."""
    monkeypatch.setattr(api, "RecipientBridge", lambda **kw: RecipientBridge(
        **kw, desktop=env.desktop, claude=env.claude))
    async with gateway(tmp_path, monkeypatch) as (_, keys, stores, adapter, client):
        auth = {"Authorization": "Bearer " + keys["alpha"]}
        base = "/p/alpha/v1/recipient-bridge/"
        common = {"session_id": "same-session", "actor_scope": "operator"}

        async def call(operation, **arguments):
            response = await client.post(base + operation, headers=auth, json={**common, **arguments})
            return response, await response.json()

        yield SimpleNamespace(call=call, client=client, auth=auth, adapter=adapter, stores=stores)


@pytest.mark.asyncio
async def test_capability_descriptor_adds_read_only_history_without_dropping_control_operations(
        tmp_path, monkeypatch, native_sources):
    async with routed(tmp_path, monkeypatch, native_sources) as box:
        response = await box.client.get("/p/alpha/v1/capabilities", headers=box.auth)
        assert response.status == 200
        capability = (await response.json())["features"]["recipient_bridge"]
        assert capability["version"] == 1
        assert capability["completion_tracking"] is False
        assert capability["two_phase_send"] is True and capability["existing_tasks_only"] is True
        # The read-only operations are additive: every existing control operation survives.
        assert capability["operations"] == [
            "list", "select", "send", "reconcile", "inspect", "catalog", "history", "status"]
        history = capability["history"]
        assert history["read_only"] is True
        assert history["sources"] == ["codex_native_history", "claude_session_history"]
        assert (history["default_limit"], history["max_limit"]) == (
            recipient_history.DEFAULT_LIMIT, recipient_history.MAX_LIMIT) == (20, 50)
        assert history["token_seconds"] == recipient_history.TOKEN_SECONDS == 300
        assert history["page_characters"] == recipient_history.PAGE_CHARACTERS == 32000
        assert history["message_characters"] == 8000
        assert (history["history_bytes"], history["identity_prefix_bytes"]) == (
            sources.HISTORY_BYTES, sources.PREFIX_BYTES) == (4 * 1024 * 1024, 64 * 1024)
        assert (history["catalog_candidates"], history["catalog_records"]) == (1000, 200)


@pytest.mark.asyncio
async def test_stored_catalog_entries_never_inherit_live_control_permissions(
        tmp_path, monkeypatch, native_sources):
    """Same app, same task id, same title - and still two different permission sets."""
    env = native_sources
    async with routed(tmp_path, monkeypatch, env) as box:
        _, live = await box.call("list", app="codex_desktop")
        _, stored = await box.call("catalog", app="codex_desktop", limit=1)
        controlled, read_only = live["recipients"][0], stored["recipients"][0]
        assert controlled["task_id"] == read_only["task_id"] == TASKS[0]
        assert controlled["title"] == read_only["title"]
        assert controlled["target_token"] != read_only["target_token"]
        assert read_only["read_only"] is True and read_only["proven_control"] == "none"
        assert read_only["operations"] == ["select", "history", "status"]
        assert read_only["source"]["kind"] == "codex_native_history"
        assert read_only["source"]["read_only"] is True
        # The live token can be addressed for control; the stored one cannot, ever.
        assert "send" in controlled["operations"]
        response, payload = await box.call(
            "send", target_token=read_only["target_token"], operation_id="no-send", message="forbidden")
        assert response.status == 409 and payload["error"] == "recipient_control_unavailable"
        response, payload = await box.call(
            "inspect", target_token=read_only["target_token"], capture=True)
        assert response.status == 409 and payload["error"] == "recipient_inspection_unavailable"
        # Selecting a read-only identity does not navigate, resume or upgrade it.
        response, selected = await box.call("select", target_token=read_only["target_token"])
        assert response.status == 200 and selected["read_only"] is True
        assert selected["proven_control"] == "none"
        assert selected["operations"] == ["select", "history", "status"]
        response, payload = await box.call(
            "send", target_token=read_only["target_token"], operation_id="still-no", message="forbidden")
        assert response.status == 409 and payload["error"] == "recipient_control_unavailable"
        assert env.desktop.composes == env.desktop.submits == env.desktop.captures == 0
        assert env.native.writes == [] and env.native.secrets_read == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("app", ["codex_desktop", "claude_code"])
async def test_identically_titled_tasks_return_their_own_history_through_the_route(
        tmp_path, monkeypatch, native_sources, app):
    env = native_sources
    async with routed(tmp_path, monkeypatch, env) as box:
        _, first = await box.call("catalog", app=app, limit=1)
        _, second = await box.call("catalog", app=app, limit=1, cursor=first["next_cursor"])
        one, two = first["recipients"][0], second["recipients"][0]
        assert one["title"] == two["title"] == "same title"
        assert {one["task_id"], two["task_id"]} == set(TASKS)
        assert one["recipient_id"] != two["recipient_id"]
        assert one["source"]["source_id"] != two["source"]["source_id"]
        for target in (one, two):
            response, history = await box.call("history", target_token=target["target_token"])
            assert response.status == 200
            assert history["task_id"] == target["task_id"]
            assert history["recipient_id"] == target["recipient_id"]
            # Identity comes from the token, never from the shared display title.
            assert history["messages"][1]["text"] == "answer " + target["task_id"]
            assert str(env.paths[app, target["task_id"]]) not in json.dumps(history)


@pytest.mark.asyncio
@pytest.mark.parametrize("app", ["codex_desktop", "claude_code"])
async def test_status_says_unknown_and_names_why_completion_is_unverified(
        tmp_path, monkeypatch, native_sources, app):
    env = native_sources
    async with routed(tmp_path, monkeypatch, env) as box:
        _, catalog = await box.call("catalog", app=app, limit=1)
        target = catalog["recipients"][0]
        response, readable = await box.call("status", target_token=target["target_token"])
        assert response.status == 200
        assert readable["status"] == "unknown" and readable["completion_tracking"] is False
        assert readable["available"] is True
        # A readable final assistant message is still not proof the external task finished.
        assert readable["reason"] == "external_completion_unverified"
        assert readable["source"]["read_only"] is True and readable["observed_at"]
        env.paths[app, target["task_id"]].unlink()
        response, gone = await box.call("status", target_token=target["target_token"])
        assert response.status == 200
        assert gone["status"] == "unknown" and gone["available"] is False
        assert gone["reason"] == "native_history_unavailable" and "source" not in gone
        assert env.desktop.composes == env.desktop.submits == env.desktop.captures == 0


@pytest.mark.asyncio
async def test_the_route_maps_unknown_stale_and_unavailable_reads_to_their_own_statuses(
        tmp_path, monkeypatch, native_sources):
    env, app = native_sources, "codex_desktop"
    async with routed(tmp_path, monkeypatch, env) as box:
        _, catalog = await box.call("catalog", app=app, limit=1)
        target = catalog["recipients"][0]
        response, payload = await box.call("history", target_token="never-issued")
        assert response.status == 404 and payload["error"] == "recipient_not_found"
        response, page = await box.call("history", target_token=target["target_token"], limit=1)
        assert response.status == 200 and page["next_cursor"]
        with env.paths[app, target["task_id"]].open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(native_row(app, target["task_id"], "assistant", "later answer")) + "\n")
        response, payload = await box.call(
            "history", target_token=target["target_token"], cursor=page["next_cursor"])
        assert response.status == 409 and payload["error"] == "history_snapshot_changed"
        response, payload = await box.call(
            "history", target_token=target["target_token"], cursor="never-issued")
        assert response.status == 404 and payload["error"] == "cursor_not_found"
        env.paths[app, target["task_id"]].unlink()
        response, payload = await box.call("history", target_token=target["target_token"])
        assert response.status == 503 and payload["error"] == "native_history_unavailable"
        assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_read_only_tokens_expire_and_are_not_refreshed_by_addressing_the_task(
        tmp_path, monkeypatch, native_sources):
    env = native_sources
    monkeypatch.setattr(recipient_history, "TOKEN_SECONDS", 0)
    async with routed(tmp_path, monkeypatch, env) as box:
        _, catalog = await box.call("catalog", app="codex_desktop", limit=1)
        target = catalog["recipients"][0]
        response, payload = await box.call("history", target_token=target["target_token"])
        assert response.status == 409 and payload["error"] == "history_target_expired"
        response, payload = await box.call("status", target_token=target["target_token"])
        assert response.status == 409 and payload["error"] == "history_target_expired"
        # A client must not retry a send to refresh an expired read.
        response, payload = await box.call(
            "send", target_token=target["target_token"], operation_id="refresh", message="ping")
        assert response.status == 409 and payload["error"] != "history_target_expired"
        response, payload = await box.call("history", target_token=target["target_token"])
        assert response.status == 409 and payload["error"] == "history_target_expired"
        assert env.desktop.composes == env.desktop.submits == env.desktop.captures == 0
