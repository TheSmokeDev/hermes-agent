"""Real API → public lifecycle → real child builder/loop; provider responses are fixtures."""
import asyncio
from copy import deepcopy
import json
import secrets
import threading
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB
from passive_history_ingress import PRODUCER
from run_agent import AIAgent


async def wait_status(client, run_id, auth, expected):
    async def wait():
        while True:
            response = await client.get(f"/v1/runs/{run_id}", headers=auth)
            status = await response.json()
            if status.get("status") in expected:
                return status
            if status.get("status") in {"completed", "failed", "cancelled"}:
                raise AssertionError(status)
            await asyncio.sleep(0.02)
    return await asyncio.wait_for(wait(), timeout=15)


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse_and_stop", [False, True])
async def test_child_goal_is_separate_and_run_owns_approval_and_stop(tmp_path, monkeypatch, reuse_and_stop):
    from tests.run_agent.test_run_agent import _mock_response
    from tools.approval_context import get_current_session_key
    from tools.approval import request_tool_approval
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    db = SessionDB(root / "state.db")
    db.create_session("parent", source="test")
    original, goal, context = "Original spoken instruction", "Derived child execution goal", "Bounded child context"
    origin = {"event_id": "speech-event", "origin_turn_id": "speech-turn"}
    before = []
    if reuse_and_stop:
        receipt = db.append_passive_messages("parent", producer=PRODUCER, **origin,
            messages=[{"role": "user", "content": original}, {"role": "assistant", "content": "Spoken acknowledgement"}])
        origin["receipt_id"] = receipt.revision
        before = deepcopy(db.get_messages("parent"))
    key = secrets.token_hex(24)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": key}))
    adapter._session_db = db
    parents, children, scopes, provider_payloads = [], [], [], []
    ready, finish = threading.Event(), threading.Event()
    original_run = AIAgent.run_conversation

    def child_only(self, *args, **kwargs):
        assert self.platform == "subagent", "parent model execution is forbidden"
        assert getattr(self, "_execution_origin_claim", None) is None
        children.append(self)
        self.compression_enabled = False
        self.save_trajectories = False
        self.tool_delay = 0
        self.client = MagicMock()

        def provider_response(**request):
            scopes.append(get_current_session_key())
            provider_payloads.append(json.dumps(request["messages"]))
            if len(scopes) == 1:
                ready.set()
                decision = request_tool_approval("write_file", "fixture child approval", rule_key="child-boundary")
                if not decision["approved"]:
                    return _mock_response(content="Child approval refused", finish_reason="stop")
                while not finish.wait(0.02):
                    if self._interrupt_requested:
                        break
            return _mock_response(content="Child result", finish_reason="stop")

        self.client.chat.completions.create.side_effect = provider_response
        return original_run(self, *args, **kwargs)

    monkeypatch.setattr(AIAgent, "run_conversation", child_only)
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *a, **k: None)
    monkeypatch.setattr("tools.delegate_tool._get_worktree_isolation", lambda: False)

    def build_parent(**kwargs):
        parent = AIAgent(api_key="fixture-only-key", provider="openrouter", model="test-model",
                         base_url="https://openrouter.ai/api/v1", session_id=kwargs["session_id"],
                         session_db=db, platform="api_server", enabled_toolsets=["file"],
                         quiet_mode=True, skip_context_files=True, skip_memory=True,
                         tool_progress_callback=kwargs["tool_progress_callback"])
        parents.append(parent)
        return parent

    monkeypatch.setattr(adapter, "_create_agent", build_parent)
    app = web.Application()
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, path, handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    auth = {"Authorization": "Bearer " + key}
    headers = {**auth, "Idempotency-Key": "one-action"}
    body = {"session_id": "parent", "input": original, "origin": origin,
            "child": {"goal": goal, "context": context, "correlation_id": "dispatch-one", "allowed_toolsets": ["file"]}}
    try:
        with (patch("model_tools.get_tool_definitions", return_value=[]),
              patch("model_tools.check_toolset_requirements", return_value={}),
              patch("agent.process_bootstrap.OpenAI")):
            responses = await asyncio.gather(*(client.post("/v1/runs", headers=headers, json=body) for _ in range(2)))
            assert all(response.status == 202 for response in responses)
            ids = [(await response.json())["run_id"] for response in responses]
            assert ids[0] == ids[1]
            run_id = ids[0]
            assert await asyncio.to_thread(ready.wait, 10)
            assert scopes == [run_id]
            waiting = await wait_status(client, run_id, auth, {"waiting_for_approval"})
            assert scopes == [run_id]
            assert adapter._run_approval_sessions[run_id] == run_id
            assert len(parents) == len(children) == 1
            assert children[0].enabled_toolsets == ["file"]
            wrong = await client.post(f"/v1/runs/{run_id}/approval", headers={"Authorization": "Bearer wrong"},
                                       json={"choice": "once"})
            assert wrong.status == 401
            if reuse_and_stop:
                response = await client.post(f"/v1/runs/{run_id}/stop", headers=auth, json={})
                assert response.status == 200
                late = await client.post(f"/v1/runs/{run_id}/approval", headers=auth,
                                          json={"choice": "once", "request_id": waiting["approval"]["request_id"]})
                assert late.status == 409
            else:
                response = await client.post(f"/v1/runs/{run_id}/approval", headers=auth,
                                              json={"choice": "once", "request_id": waiting["approval"]["request_id"]})
                assert response.status == 200
                finish.set()
            terminal = await wait_status(client, run_id, auth, {"completed", "failed", "cancelled"})
            assert terminal["status"] == ("cancelled" if reuse_and_stop else "completed"), terminal
            parent_rows = db.get_messages("parent")
            if before:
                assert parent_rows == before
            else:
                assert [(row["role"], row["content"]) for row in parent_rows] == [("user", original)]
            assert terminal["parent_message_id"] == parent_rows[0]["id"]
            child_id = terminal["child_session_id"]
            assert db.get_session(child_id)["parent_session_id"] == "parent"
            assert any(row["role"] == "user" and goal in row["content"] for row in db.get_messages(child_id))
            assert context in provider_payloads[0]
            assert all(row["content"] != goal for row in parent_rows)
            assert db.get_session("parent")["api_call_count"] == 0
            replay = await client.post("/v1/runs", headers=headers, json=body)
            assert replay.status == 202 and (await replay.json())["run_id"] == run_id
            changed = {**body, "child": {**body["child"], "goal": "changed goal"}}
            assert (await client.post("/v1/runs", headers=headers, json=changed)).status == 409
            assert len(children) == 1
            # A new adapter has no active child state; its durable key still prevents replacement.
            restarted = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": key}))
            restarted._session_db = db
            new_app = web.Application()
            new_app.router.add_post("/v1/runs", restarted._handle_runs)
            peer = TestClient(TestServer(new_app))
            await peer.start_server()
            try:
                replay = await peer.post("/v1/runs", headers=headers, json=body)
                assert replay.status == 202 and (await replay.json())["run_id"] == run_id
                assert not restarted._active_run_tasks
                assert (await peer.post("/v1/runs", headers={**auth, "Idempotency-Key": "new-key-same-correlation"}, json=body)).status == 409
            finally:
                await peer.close()
                await restarted.disconnect()
    finally:
        finish.set()
        await client.close()
        await adapter.disconnect()
        db.close()
