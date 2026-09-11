"""Recorded/pending origin steering through real HTTP, queue, provider loop and canonical flush."""
import asyncio
from copy import deepcopy
import threading
from unittest.mock import MagicMock, patch

import pytest

from agent.agent_runtime_helpers import _requeue_pending_steer
from agent.prompt_builder import steer_user_row, steer_user_rows
from agent.steer_origin import bound_steer_text, unbound_steer_text
from agent.turn_iteration_prep import _inject_steer_after_newest_tool_result
from hermes_state_passive_history import PassiveHistoryBusyError
from passive_history_ingress import PRODUCER
from run_agent import AIAgent
from tests.gateway.test_dashboard_consumption import gateway
from tests.run_agent.test_run_agent import _make_tool_defs, _mock_response, _mock_tool_call
from tests.run_agent.test_steer import _bare_agent


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", [False, True])
async def test_ordinary_origin_has_one_row_and_preserves_cached_prefix(tmp_path, monkeypatch, recorded):
    ready, finish = threading.Event(), threading.Event()
    agents, payloads = [], []
    real_run = AIAgent.run_conversation
    text, wire = "Please use the revised constraint", "Exact existing API correction bytes"
    monkeypatch.setattr("agent.model_metadata.fetch_model_metadata", lambda *a, **k: {})
    async with gateway(tmp_path, monkeypatch) as (_, keys, stores, adapter, client):
        db = stores["alpha"]
        origin = {"event_id": "correction-event", "origin_turn_id": "correction-turn"}
        deleted = db.append_passive_messages("same-session", producer=PRODUCER,
            event_id="deleted-event", origin_turn_id="deleted-turn",
            messages=[{"role": "user", "content": "Deleted input"}])
        db._execute_write(lambda conn: conn.execute("DELETE FROM messages WHERE id=?", (deleted.message_ids[0],)))
        db.create_session("foreign-session", source="test")
        foreign = db.append_passive_messages("foreign-session", producer=PRODUCER,
            event_id="foreign-event", origin_turn_id="foreign-turn",
            messages=[{"role": "user", "content": text}])
        prior_row = None
        if recorded:
            prior = db.append_passive_messages("same-session", producer=PRODUCER, **origin,
                messages=[{"role": "user", "content": text}])
            origin["receipt_id"] = prior.revision
            db.set_message_api_content("same-session", prior.message_ids[0], text, wire)
            prior_row = deepcopy(db.get_messages("same-session")[0])
        def run(self, *args, **kwargs):
            self.compression_enabled, self.save_trajectories, self.tool_delay = False, False, 0
            self.client = MagicMock()
            def response(**request):
                payloads.append(deepcopy(request["messages"]))
                if len(payloads) == 1:
                    ready.set()
                    assert finish.wait(15)
                    return _mock_response(content="", finish_reason="tool_calls",
                        tool_calls=[_mock_tool_call("read_file", '{"path":"fixture"}')])
                return _mock_response(content="Updated answer", finish_reason="stop")
            self.client.chat.completions.create.side_effect = response
            return real_run(self, *args, **kwargs)
        monkeypatch.setattr(AIAgent, "run_conversation", run)
        monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *a, **k: None)
        def parent(**kwargs):
            agent = AIAgent(api_key="fixture-only", provider="openrouter", model="fixture",
                base_url="https://openrouter.ai/api/v1", session_id=kwargs["session_id"], session_db=db,
                platform="api_server", enabled_toolsets=["file"], quiet_mode=True, skip_context_files=True,
                skip_memory=True, tool_progress_callback=kwargs["tool_progress_callback"])
            agents.append(agent)
            return agent
        monkeypatch.setattr(adapter, "_create_agent", parent)
        auth = {"Authorization": "Bearer " + keys["alpha"]}
        try:
            with (patch("model_tools.get_tool_definitions", return_value=_make_tool_defs("read_file")),
                  patch("model_tools.check_toolset_requirements", return_value={}),
                  patch("model_tools.handle_function_call", return_value="Fixture tool response"),
                  patch("agent.process_bootstrap.OpenAI")):
                start = await client.post("/p/alpha/v1/runs", headers={**auth, "Idempotency-Key": "ordinary"},
                    json={"session_id": "same-session", "input": "Continue the original job"})
                assert start.status == 202, await start.text()
                run_id = (await start.json())["run_id"]
                assert await asyncio.to_thread(ready.wait, 10)
                path = f"/p/alpha/v1/runs/{run_id}/steer"
                target = await (await client.get(path, headers=auth)).json()
                control = {"version": 1, "action_id": "origin-action", "expected_session_id": target["session_id"],
                           "expected_turn_id": target["turn_id"], "origin": origin}
                body = {"input": text, "control": control}
                for label, candidate, candidate_text in (
                    ("deleted", {"event_id": "deleted-event", "origin_turn_id": "deleted-turn", "receipt_id": deleted.revision}, "Deleted input"),
                    ("foreign-origin", {"event_id": "foreign-event", "origin_turn_id": "foreign-turn", "receipt_id": foreign.revision}, text),
                    ("missing-origin", {**origin, "receipt_id": 999999}, text),
                ):
                    refused = await (await client.post(path, headers=auth, json={"input": candidate_text,
                        "control": {**control, "action_id": label, "origin": candidate}})).json()
                    assert refused["status"] == "rejected", refused
                # A competing action reservation must win before any pending origin is written.
                store = adapter._run_idempotency_store
                real_receipt = store.steer_receipt
                def conflict_on_reserve(*args, **kwargs):
                    return ("conflict", None) if kwargs.get("reserve") else real_receipt(*args, **kwargs)
                losing_origin = {"event_id": "losing-event", "origin_turn_id": "losing-turn"}
                with patch.object(store, "steer_receipt", side_effect=conflict_on_reserve):
                    assert (await client.post(path, headers=auth, json={**body, "control": {
                        **control, "action_id": "losing-action", "origin": losing_origin}})).status == 409
                assert db.get_passive_history_receipt("same-session", producer=PRODUCER, event_id="losing-event") is None
                stale = {**body, "control": {**control, "action_id": "stale", "expected_turn_id": "old"}}
                assert (await (await client.post(path, headers=auth, json=stale)).json())["status"] == "rejected"
                if not recorded:
                    with pytest.raises(PassiveHistoryBusyError):
                        db.append_passive_messages("same-session", producer=PRODUCER, **origin,
                            messages=[{"role": "user", "content": text}])
                    assert db.get_passive_history_receipt("same-session", producer=PRODUCER, event_id=origin["event_id"]) is None
                assert (await client.post(path.replace("/alpha/", "/beta/"), headers={
                    "Authorization": "Bearer " + keys["beta"]}, json=body)).status == 404
                old_holder = agents[0]._active_session_turn_lease_holder
                agents[0]._active_session_turn_lease_holder = "foreign-holder"
                try:
                    rejected = await (await client.post(path, headers=auth, json={**body,
                        "control": {**control, "action_id": "foreign-lease"}})).json()
                    assert rejected["status"] == "rejected", rejected
                finally:
                    agents[0]._active_session_turn_lease_holder = old_holder
                responses = await asyncio.gather(*(client.post(path, headers=auth, json=body) for _ in range(2)))
                receipts = [await response.json() for response in responses]
                assert receipts[0] == receipts[1] and receipts[0]["status"] == "queued", receipts
                delayed = db.append_passive_messages("same-session", producer=PRODUCER,
                    event_id=origin["event_id"], origin_turn_id=origin["origin_turn_id"],
                    messages=[{"role": "user", "content": text}])
                assert delayed.message_ids == (receipts[0]["parent_message_id"],)
                assert delayed.revision == receipts[0]["origin"]["receipt_id"]
                # Queue admission followed by a lost outcome remains unknown and never requeues.
                uncertain_text = "Retain the original deadline"
                uncertain = {"input": uncertain_text, "control": {**control, "action_id": "uncertain", "origin": {
                    "event_id": "uncertain-event", "origin_turn_id": "uncertain-turn"}}}
                with patch.object(store, "settle_steer_receipt", side_effect=RuntimeError("fixture settlement failure")):
                    assert (await client.post(path, headers=auth, json=uncertain)).status == 503
                unknown = await (await client.post(path, headers=auth, json=uncertain)).json()
                assert unknown["status"] == "unknown" and unknown["evidence"] == "reserved_before_queue"
                assert await (await client.get(path + "?action_id=uncertain", headers=auth)).json() == unknown
                # Mixed legacy input uses the same queue and retains its own ordinary persistence.
                assert (await client.post(path, headers=auth, json={"input": "Legacy follow-up"})).status == 200
                assert await (await client.get(path + "?action_id=origin-action", headers=auth)).json() == receipts[0]
                assert (await client.post(path, headers=auth, json={**body, "input": "Changed correction"})).status == 409
                finish.set()
                async def complete():
                    while True:
                        state = await (await client.get(f"/p/alpha/v1/runs/{run_id}", headers=auth)).json()
                        if state.get("status") in {"completed", "failed"}:
                            assert state["status"] == "completed", state
                            return state
                        await asyncio.sleep(0.01)
                final = await asyncio.wait_for(complete(), 10)
                assert final["run_id"] == run_id and len(agents) == 1
                assert len(payloads) == 2 and payloads[1][:len(payloads[0])] == payloads[0]
                rows = db.get_messages("same-session")
                actual = [row for row in rows if row["content"] == text]
                assert len(actual) == 1 and actual[0]["id"] == delayed.message_ids[0]
                if prior_row:
                    assert actual[0] == prior_row
                    assert payloads[0][-1]["content"] == wire + "\n\nContinue the original job"
                tail = payloads[1][len(payloads[0]):]
                # The provider's existing alternation adapter joins adjacent user copies.
                # Check the entire composed wire value, including the exact origin bytes.
                assert tail[-1]["content"] == "\n\n".join([
                    wire if recorded else text, uncertain_text, steer_user_row("Legacy follow-up")["content"]])
                assert sum(row["content"] == uncertain_text for row in rows) == 1
                assert sum(row.get("display_kind") == "steer" and "Legacy follow-up" in row["content"] for row in rows) == 1
                assert await (await client.post(path, headers=auth, json=body)).json() == receipts[0]
                assert (await (await client.post(path, headers=auth, json={**body,
                    "control": {**control, "action_id": "terminal"}})).json())["status"] == "rejected"
        finally:
            finish.set()


def test_binding_survives_requeue_without_prefix_edits_or_next_turn_replay():
    agent = _bare_agent()
    row = {"role": "user", "content": "Canonical input", "api_content": "Exact wire bytes",
           "_db_persisted": True, "_row_id": 123}
    value = bound_steer_text("Canonical input", row)
    assert agent.steer(value)
    drained = agent._drain_pending_steer()
    assert agent.steer("Legacy correction")
    _requeue_pending_steer(agent, drained)
    combined = agent._drain_pending_steer()
    assert steer_user_rows(combined)[-1] == row
    assert unbound_steer_text(combined) == "Legacy correction"
    assert unbound_steer_text(value) is None
    prefix = [{"role": "tool", "content": "Old cached result"}, {"role": "user", "content": "Current request"}]
    before = deepcopy(prefix)
    _inject_steer_after_newest_tool_result(agent, prefix, value)
    assert prefix == before
    assert steer_user_rows(agent._drain_pending_steer()) == [row]
    _inject_steer_after_newest_tool_result(agent, prefix, value)
    prefix.append({"role": "tool", "content": "Fresh result"})
    _inject_steer_after_newest_tool_result(agent, prefix, agent._drain_pending_steer())
    assert prefix[:-1] == before + [{"role": "tool", "content": "Fresh result"}]
    assert prefix[-1] == row
