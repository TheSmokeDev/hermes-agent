"""Room access retires independently of accepted canonical worker ownership."""
import asyncio
from unittest.mock import patch

import pytest
from aiohttp import web

from agent import task_worker_registry
from gateway.platforms.api_server_discord_context import PROOF_HEADER
from hermes_cli.plugins import PluginManager
from run_agent import AIAgent
from tests.gateway.test_api_task_workers import PLUGIN
from tests.gateway.test_dashboard_consumption import gateway
from tests.gateway.test_discord_task_context import binding, room_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["close", "audience", "approval_audience", "rebind", "expire"])
async def test_room_revocation_preserves_accepted_worker_and_canonical_result(tmp_path, monkeypatch, retirement):
    task_worker_registry._reset_for_tests()
    monkeypatch.setattr(AIAgent, "run_conversation", lambda *a, **kw: pytest.fail("parent model ran"))
    monkeypatch.setattr("agent.model_metadata.fetch_model_metadata", lambda *a, **kw: {})
    room = room_fixture()
    async with gateway(tmp_path, monkeypatch) as (root, keys, stores, adapter, client):
        room.runner.config = adapter.gateway_runner.config
        adapter.gateway_runner = room.runner
        home = root / "profiles" / "alpha"
        plugin = home / "plugins" / "fixture-worker"
        plugin.mkdir(parents=True)
        (plugin / "plugin.yaml").write_text("name: fixture-worker\nversion: 1.0.0\ndescription: fixture\n")
        (plugin / "__init__.py").write_text(PLUGIN)
        (home / "config.yaml").write_text("plugins:\n  enabled: [fixture-worker]\n")
        with adapter._profile_scope("alpha"):
            manager = PluginManager()
            manager.discover_and_load()
            provider = task_worker_registry.configured_worker("fixture-worker")
        db = stores["alpha"]
        def parent(**kwargs):
            return AIAgent(api_key="fixture-key", provider="openrouter", model="fixture",
                base_url="https://openrouter.ai/api/v1", session_id=kwargs["session_id"], session_db=db,
                platform="api_server", enabled_toolsets=["file"], quiet_mode=True, skip_context_files=True,
                skip_memory=True, tool_progress_callback=kwargs["tool_progress_callback"])
        monkeypatch.setattr(adapter, "_create_agent", parent)
        auth = {"Authorization": "Bearer " + keys["alpha"]}
        context_path = "/p/alpha/v1/task-context/discord/"
        context = binding(room)
        assert (await client.post(context_path + "redeem", headers=auth, json=context)).status == 200
        voice_auth = {**auth, PROOF_HEADER: context["proof"]}
        body = {"session_id": "same-session", "input": "Exact room request",
                "origin": {"event_id": "room-event", "origin_turn_id": "room-turn"},
                "child": {"goal": "Derived goal", "correlation_id": "room-action", "worker": "fixture-worker"},
                "discord_task_context": context}
        session = None
        try:
            invalid = {**body, "discord_task_context": {**context, "proof": "forged"}}
            refused = await client.post("/p/alpha/v1/runs", headers={**auth, "Idempotency-Key": "refused"}, json=invalid)
            assert refused.status == 404, await refused.text()
            assert db.get_messages("same-session") == []
            with (patch("model_tools.get_tool_definitions", return_value=[]),
                  patch("model_tools.check_toolset_requirements", return_value={}),
                  patch("agent.process_bootstrap.OpenAI")):
                response = await client.post("/p/alpha/v1/runs", headers={**voice_auth, "Idempotency-Key": "room-job"}, json=body)
                assert response.status == 202, await response.text()
                run_id = (await response.json())["run_id"]
                path = "/p/alpha/v1/runs/" + run_id
                async def status_in(states):
                    while True:
                        state = await (await client.get(path, headers=auth)).json()
                        if state.get("status") in states:
                            return state
                        if state.get("status") in {"failed", "completed", "cancelled"}:
                            raise AssertionError(state)
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(status_in({"waiting_for_approval"}), 10)
                session = provider.sessions[0]
                assert session.request.room_context["audience_user_ids"] == ("1", "2")
                assert "proof" not in session.request.room_context
                with pytest.raises(TypeError):
                    session.request.room_context["channel_id"] = "forged"
                pending = await (await client.get(path + "/approval", headers=voice_auth)).json()
                assert pending["approvals"][0]["request_id"] == "approval-one"
                broad = await client.post(path + "/approval", headers=voice_auth, json={"request_id": "approval-one", "choice": "session"})
                assert broad.status == 409 and session.decisions == []
                if retirement == "close":
                    assert (await client.post(context_path + "revoke", headers=auth, json=context)).status == 200
                elif retirement == "audience":
                    room.add(3)
                elif retirement == "approval_audience":
                    original_json = web.Request.json
                    async def change_during_body(request, *args, **kwargs):
                        value = await original_json(request, *args, **kwargs)
                        if request.path == path + "/approval":
                            room.add(3)
                        return value
                    with monkeypatch.context() as change:
                        change.setattr(web.Request, "json", change_during_body)
                        refused = await client.post(path + "/approval", headers=voice_auth,
                                                   json={"request_id": "approval-one", "choice": "once"})
                    assert refused.status == 403 and session.decisions == []
                elif retirement == "rebind":
                    db.create_session("new-target", source="test")
                    moved = await client.post(context_path + "rebind", headers=auth, json={**context,
                        "next_session_id": "new-target", "next_binding_id": "next-native"})
                    assert moved.status == 200, await moved.text()
                else:
                    room.now[0] += 901
                expected_status = 403 if retirement == "audience" else 409
                assert (await client.get(path, headers=voice_auth)).status == expected_status
                assert (await client.get(path + "/approval", headers=voice_auth)).status == 409
                assert (await client.post(path + "/steer", headers=voice_auth, json={"input": "untrusted"})).status == 409
                assert session.request.still_authorized() and not session.cancelled
                assert db.get_messages("same-session")[0]["content"] == body["input"]
                accepted = await client.post(path + "/approval", headers=auth, json={"request_id": "approval-one", "choice": "once"})
                assert accepted.status == 200, await accepted.text()
                assert session.decisions == ["once"]
                session.finish.set()
                final = await asyncio.wait_for(status_in({"completed"}), 10)
                assert final["status"] == "completed"
                assert [row["content"] for row in db.get_messages(session.request.child_session_id)] == [
                    "Derived goal", "Full external result"]
        finally:
            if session is not None:
                session.finish.set()
            if adapter._active_run_tasks:
                await asyncio.gather(*list(adapter._active_run_tasks.values()), return_exceptions=True)
            manager.unload()
            task_worker_registry._reset_for_tests()


@pytest.mark.asyncio
async def test_sse_stops_before_next_frame_after_voice_audience_changes(tmp_path, monkeypatch):
    room = room_fixture()
    async with gateway(tmp_path, monkeypatch) as (_, keys, _, adapter, client):
        room.runner.config = adapter.gateway_runner.config
        adapter.gateway_runner = room.runner
        auth = {"Authorization": "Bearer " + keys["alpha"]}
        context = binding(room)
        response = await client.post("/p/alpha/v1/task-context/discord/redeem", headers=auth, json=context)
        assert response.status == 200
        record = room.contexts.proofs[context["proof"]]
        run_id = "run-room-stream"
        adapter._run_owners[run_id] = record.owner
        adapter._set_run_status(run_id, "running", session_id="same-session")
        queue = adapter._run_streams[run_id] = asyncio.Queue()
        queue.put_nowait({"event": "fixture.first", "value": "allowed"})
        stream = await client.get("/p/alpha/v1/runs/" + run_id + "/events", headers={**auth, PROOF_HEADER: context["proof"]})
        assert stream.status == 200
        first = await asyncio.wait_for(stream.content.readuntil(b"\n\n"), 5)
        assert b"allowed" in first
        room.add(3)
        queue.put_nowait({"event": "fixture.secret", "value": "must not reach changed room"})
        remaining = await asyncio.wait_for(stream.content.read(), 5)
        assert b"must not reach changed room" not in remaining
        assert adapter._run_statuses[run_id]["status"] == "running"
