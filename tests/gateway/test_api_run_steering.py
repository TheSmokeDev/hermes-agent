"""Same owning run, real loop/queue/persistence, exact origin and durable control replay."""
import asyncio
from copy import deepcopy
from unittest.mock import MagicMock, patch
import threading

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from passive_history_ingress import PRODUCER
from run_agent import AIAgent
from tests.gateway.test_dashboard_consumption import gateway
from tests.run_agent.test_run_agent import _make_tool_defs, _mock_response, _mock_tool_call


@pytest.mark.asyncio
@pytest.mark.parametrize("linked", [False, True])
async def test_same_run_control_origin_and_durable_replay(tmp_path, monkeypatch, linked):
    ready, finish = threading.Event(), threading.Event()
    parents, executing, payloads = [], [], []
    real_run = AIAgent.run_conversation

    def with_provider(self, *args, **kwargs):
        if linked:
            assert self.platform == "subagent", "linked steering must never start the parent model"
        executing.append(self)
        self.compression_enabled, self.save_trajectories, self.tool_delay = False, False, 0
        self.client = MagicMock()
        calls = 0
        def response(**request):
            nonlocal calls
            calls += 1
            payloads.append(deepcopy(request["messages"]))
            if calls == 1:
                ready.set()
                assert finish.wait(15)
                return _mock_response(content="", finish_reason="tool_calls",
                    tool_calls=[_mock_tool_call("read_file", '{"path":"fixture"}')])
            return _mock_response(content="Corrected result", finish_reason="stop")
        self.client.chat.completions.create.side_effect = response
        return real_run(self, *args, **kwargs)

    monkeypatch.setattr(AIAgent, "run_conversation", with_provider)
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *a, **k: None)
    monkeypatch.setattr("tools.delegate_tool._get_worktree_isolation", lambda: False)
    async with gateway(tmp_path, monkeypatch) as (_, keys, stores, adapter, client):
        db = stores["alpha"]
        def parent(**kwargs):
            agent = AIAgent(api_key="fixture-only", provider="openrouter", model="test-model",
                base_url="https://openrouter.ai/api/v1", session_id=kwargs["session_id"], session_db=db,
                platform="api_server", enabled_toolsets=["file"], quiet_mode=True,
                skip_context_files=True, skip_memory=True, tool_progress_callback=kwargs["tool_progress_callback"])
            parents.append(agent)
            return agent
        monkeypatch.setattr(adapter, "_create_agent", parent)
        auth = {"Authorization": "Bearer " + keys["alpha"]}
        body = {"session_id": "same-session", "input": "Original user request"}
        if linked:
            body.update(origin={"event_id": "initial-event", "origin_turn_id": "initial-turn"},
                        child={"goal": "Derived goal", "correlation_id": "child-action"})
        try:
            with (patch("model_tools.get_tool_definitions", return_value=_make_tool_defs("read_file")),
                  patch("model_tools.check_toolset_requirements", return_value={}),
                  patch("model_tools.handle_function_call", return_value="fixture tool result"),
                  patch("agent.process_bootstrap.OpenAI")):
                response = await client.post("/p/alpha/v1/runs", headers={**auth, "Idempotency-Key": "job"}, json=body)
                assert response.status == 202, await response.text()
                run_id = (await response.json())["run_id"]
                assert await asyncio.to_thread(ready.wait, 10)
                route = f"/p/alpha/v1/runs/{run_id}/steer"
                # A child can enter its provider before launch has returned/published its handle.
                async def target_ready():
                    while True:
                        target = await (await client.get(route, headers=auth)).json()
                        if target.get("supported"):
                            return target
                        await asyncio.sleep(0.01)
                target = await asyncio.wait_for(target_ready(), 5)
                assert target["kind"] == ("linked_child" if linked else "ordinary")
                assert target["session_id"] == executing[0].session_id
                assert target["turn_id"] == executing[0]._current_turn_id
                def control(action, text="Use the corrected constraint"):
                    return {"input": text, "control": {"version": 1, "action_id": action,
                        "expected_session_id": target["session_id"], "expected_turn_id": target["turn_id"]}}
                first = control("correction-one")
                original_rows = deepcopy(db.get_messages("same-session"))
                if linked:
                    receipt = db.append_passive_messages("same-session", producer=PRODUCER,
                        event_id="correction-event", origin_turn_id="correction-turn",
                        messages=[{"role": "user", "content": first["input"]}])
                    first["control"]["origin"] = {"event_id": "correction-event",
                        "origin_turn_id": "correction-turn", "receipt_id": receipt.revision}
                    # A simultaneously persisted typed utterance is a separate origin, not another launch.
                    db.append_passive_messages("same-session", producer=PRODUCER, event_id="typed-event",
                        origin_turn_id="typed-turn", messages=[{"role": "user", "content": "Concurrent typed input"}])
                    for action, change in [("changed", {"input": "Changed origin text"}),
                                           ("foreign", {"control": {**first["control"], "origin": {
                                               "event_id": "other-event", "origin_turn_id": "other-turn", "receipt_id": 999}}})]:
                        refused = {**deepcopy(first), **change}
                        refused["control"]["action_id"] = action
                        refused_result = await (await client.post(route, headers=auth, json=refused)).json()
                        assert refused_result.get("status") == "rejected", refused_result
                    deleted = db.append_passive_messages("same-session", producer=PRODUCER,
                        event_id="deleted-event", origin_turn_id="deleted-turn",
                        messages=[{"role": "user", "content": "Deleted correction"}])
                    db._execute_write(lambda conn: conn.execute("DELETE FROM messages WHERE id=?", (deleted.message_ids[0],)))
                    deleted_control = control("deleted", "Deleted correction")
                    deleted_control["control"]["origin"] = {"event_id": "deleted-event",
                        "origin_turn_id": "deleted-turn", "receipt_id": deleted.revision}
                    deleted_result = await (await client.post(route, headers=auth, json=deleted_control)).json()
                    assert deleted_result.get("status") == "rejected", deleted_result
                else:
                    unsupported = control("ordinary-persisted-origin")
                    unsupported["control"]["origin"] = {"event_id": "event", "origin_turn_id": "turn", "receipt_id": 1}
                    unsupported_result = await (await client.post(route, headers=auth, json=unsupported)).json()
                    assert unsupported_result.get("status") == "unsupported", unsupported_result
                responses = await asyncio.gather(*(client.post(route, headers=auth, json=first) for _ in range(2)))
                receipts = [await response.json() for response in responses]
                assert receipts[0] == receipts[1] and receipts[0]["status"] == "queued", receipts
                assert receipts[0]["evidence"] == "backend_queue_ack"
                # Lost response: authoritative GET and repeated POST observe identical evidence.
                assert await (await client.get(route + "?action_id=correction-one", headers=auth)).json() == receipts[0]
                assert await (await client.post(route, headers=auth, json=first)).json() == receipts[0]
                assert (await client.post(route, headers=auth, json={**first, "input": "changed"})).status == 409
                stale = control("stale")
                stale["control"]["expected_turn_id"] = "previous-turn"
                assert (await (await client.post(route, headers=auth, json=stale)).json())["status"] == "rejected"
                assert (await client.post(route, headers={"Authorization": "Bearer invalid"}, json=first)).status == 401
                assert (await client.get(route.replace("/alpha/", "/beta/"), headers={
                    "Authorization": "Bearer " + keys["beta"]})).status == 404
                # Failure after a real queue acknowledgement leaves a durable unknown, never requeued.
                uncertain = control("uncertain", "Also keep the result concise")
                with patch.object(adapter._run_idempotency_store, "settle_steer_receipt", side_effect=RuntimeError("fixture")):
                    assert (await client.post(route, headers=auth, json=uncertain)).status == 503
                unknown = await (await client.post(route, headers=auth, json=uncertain)).json()
                assert unknown["status"] == "unknown" and unknown["evidence"] == "reserved_before_queue"
                assert executing[0]._pending_steer == first["input"] + "\n" + uncertain["input"]
                if linked:
                    assert parents[0]._pending_steer is None
                assert len(executing) == len(parents) == 1
                finish.set()
                async def completed():
                    while True:
                        final = await (await client.get(f"/p/alpha/v1/runs/{run_id}", headers=auth)).json()
                        if final.get("status") in {"completed", "failed", "cancelled"}:
                            assert final["status"] == "completed", final
                            return final
                        await asyncio.sleep(0.01)
                final = await asyncio.wait_for(completed(), 10)
                assert final["run_id"] == run_id
                # Stock loop drained into canonical persistence, and earlier provider prefix stayed byte-stable.
                assert len(payloads) == 2 and payloads[1][:len(payloads[0])] == payloads[0]
                rows = db.get_messages(executing[0].session_id)
                corrections = [row for row in rows if row.get("display_kind") == "steer"]
                assert len(corrections) == 1
                assert corrections[0]["content"].count(first["input"]) == 1
                assert corrections[0]["content"].count(uncertain["input"]) == 1
                if linked:
                    parent_rows = db.get_messages("same-session")
                    assert parent_rows[:len(original_rows)] == original_rows
                    assert sum(row["content"] == first["input"] for row in parent_rows) == 1
                    assert sum(row["content"] == "Concurrent typed input" for row in parent_rows) == 1
                assert (await (await client.post(route, headers=auth, json=control("late"))).json())["status"] == "rejected"
                assert await (await client.post(route, headers=auth, json=first)).json() == receipts[0]
                # New adapter/process-local state: durable outcome and unknown survive; no execution.
                restarted = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": keys["alpha"]}))
                restarted.gateway_runner = adapter.gateway_runner
                app = web.Application(middlewares=[restarted._make_profile_prefix_middleware()])
                for method, path, handler in restarted._http_route_table():
                    app.router.add_route(method, "/p/{profile}" + path, handler)
                peer = TestClient(TestServer(app))
                await peer.start_server()
                try:
                    assert await (await peer.post(route, headers=auth, json=first)).json() == receipts[0]
                    assert await (await peer.get(route + "?action_id=uncertain", headers=auth)).json() == unknown
                    assert not restarted._active_run_tasks and len(executing) == 1
                finally:
                    await peer.close()
                    await restarted.disconnect()
        finally:
            finish.set()
