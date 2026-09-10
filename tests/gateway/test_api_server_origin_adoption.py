"""Real authenticated run admission, loop persistence and read-only origin adoption."""
import asyncio
from copy import deepcopy
import secrets
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms import api_server_runs
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB
from passive_history_ingress import PRODUCER
from run_agent import AIAgent


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["missing_store", "sqlite"])
async def test_pre_dispatch_refusal_replays_terminal_error_not_phantom_acceptance(tmp_path, monkeypatch, refusal):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conversation", source="test")
    key = secrets.token_hex(24)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": key}))
    adapter._session_db = db
    auth = {"Authorization": "Bearer " + key, "Idempotency-Key": "stable-refused-request"}
    body = {"session_id": "conversation", "input": "original request",
            "origin": {"event_id": "refused-event", "origin_turn_id": "refused-origin"}}

    def forbidden(*args, **kwargs):
        raise AssertionError("refused admission must not schedule execution")

    monkeypatch.setattr(api_server_runs, "_execute_run", forbidden)
    app = web.Application()
    app.router.add_post("/v1/runs", adapter._handle_runs)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        with monkeypatch.context() as failing:
            if refusal == "missing_store":
                async def unavailable():
                    return None
                failing.setattr(adapter, "_ensure_session_db_async", unavailable)
            else:
                def busy(*args, **kwargs):
                    raise sqlite3.OperationalError("injected reservation refusal")
                failing.setattr(db, "reserve_execution_origin", busy)
            response = await client.post("/v1/runs", headers=auth, json=body)
            assert response.status == 503
            refused = await response.json()
        # Even after storage recovers, this key must not replay a run that was never scheduled.
        replay = await client.post("/v1/runs", headers=auth, json=body)
        assert replay.status == 503
        assert await replay.json() == refused
        assert refused["retryable"] is False
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert not adapter._active_run_tasks and not adapter._run_streams
        assert db.get_messages("conversation") == []
    finally:
        await client.close()
        await adapter.disconnect()
        db.close()


@pytest.mark.asyncio
async def test_tip_handoff_keeps_run_scoped_approval_and_operator_memory_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", source="test")
    db.end_session("parent", "compression")
    db.create_session("tip", source="test", parent_session_id="parent")
    db.append_message("tip", "user", "carried context")
    key = secrets.token_hex(24)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": key}))
    adapter._session_db = db
    seen, dispatched = [], asyncio.Event()

    async def inspect_dispatch(owner, run, **kwargs):
        seen.append(run)
        dispatched.set()

    monkeypatch.setattr(api_server_runs, "_execute_run", inspect_dispatch)
    app = web.Application()
    app.router.add_post("/v1/runs", adapter._handle_runs)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post("/v1/runs", headers={
            "Authorization": "Bearer " + key, "Idempotency-Key": "compression-origin",
            "X-Hermes-Session-Key": "operator-memory"}, json={
                "session_id": "parent", "input": "original request",
                "origin": {"event_id": "compressed-event", "origin_turn_id": "compressed-origin"}})
        assert response.status == 202
        run_id = (await response.json())["run_id"]
        await asyncio.wait_for(dispatched.wait(), timeout=5)
        run = seen[0]
        assert run.session_id == run.agent_kwargs["session_id"] == run.origin_claim["session_id"] == "tip"
        assert run.conversation_history[0]["content"] == "carried context"
        assert run.approval_session_key == adapter._run_approval_sessions[run_id] == run_id
        assert run.declared_selected is False
        assert run.gateway_session_key == "operator-memory"
    finally:
        for run in seen:
            api_server_runs._retire_live_run(adapter, run.run_id)
        await client.close()
        await adapter.disconnect()
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_first_write", [False, True])
async def test_accepted_origin_waits_for_real_user_row(tmp_path, monkeypatch, failed_first_write):
    from tests.run_agent.test_run_agent import _mock_response
    root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(root))
    keys = {name: secrets.token_hex(24) for name in ("alpha", "beta")}
    stores = {}
    for name, key in keys.items():
        home = root / "profiles" / name
        home.mkdir(parents=True)
        (home / ".env").write_text(f"API_SERVER_KEY={key}\n", encoding="utf-8")
        (home / "config.yaml").write_text("model:\n  default: test-model\n", encoding="utf-8")
        stores[name] = SessionDB(home / "state.db")
        stores[name].create_session("conversation", source="test")
    db = stores["alpha"]
    db.append_messages_batch("conversation", [
        {"role": "user", "content": "prior question", "api_content": "  prior user wire\n"},
        {"role": "assistant", "content": "prior answer", "api_content": "  prior assistant wire\n"},
    ])
    prefix = deepcopy(db.get_messages("conversation"))
    with (patch("model_tools.get_tool_definitions", return_value=[]),
          patch("model_tools.check_toolset_requirements", return_value={}),
          patch("agent.process_bootstrap.OpenAI")):
        agent = AIAgent(api_key="fixture-only-key", base_url="https://openrouter.ai/api/v1",
                        provider="openrouter", model="test-model", session_id="conversation",
                        session_db=db, quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent.client = MagicMock()
    agent._cached_system_prompt = "cached system prompt\n"
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.tool_delay = 0
    monkeypatch.setattr(agent, "_cleanup_task_resources", lambda: None)
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *a, **k: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("adoption cannot dispatch a tool or approval")

    monkeypatch.setattr("model_tools.handle_function_call", forbidden)
    monkeypatch.setattr("tools.approval.submit_pending", forbidden)
    payload = {"session_id": "conversation", "event_id": "event", "origin_turn_id": "utterance",
               "messages": [{"role": "user", "content": "original spoken request"}]}
    provider_saw = []

    def response(**kwargs):
        provider_saw.append(db.adopt_execution_origin(
            "conversation", producer=PRODUCER, event_id="event", origin_turn_id="utterance",
            content="original spoken request")["status"])
        return _mock_response(content="answer to original request", finish_reason="stop")

    agent.client.chat.completions.create.side_effect = response
    real_append, failed = db.append_messages_batch, []

    def append(*args, **kwargs):
        if failed_first_write and not failed and kwargs.get("_execution_origin"):
            failed.append(True)
            raise sqlite3.OperationalError("injected first write failure")
        return real_append(*args, **kwargs)

    monkeypatch.setattr(db, "append_messages_batch", append)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": keys["alpha"]}))
    adapter.gateway_runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True))
    constructions = []

    def create_agent(**kwargs):
        constructions.append(kwargs)
        return agent

    monkeypatch.setattr(adapter, "_create_agent", create_agent)
    release = asyncio.Event()
    original_execute = api_server_runs._execute_run

    async def delayed_execute(owner, run, **kwargs):
        await release.wait()
        await original_execute(owner, run, **kwargs)

    monkeypatch.setattr(api_server_runs, "_execute_run", delayed_execute)
    app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, "/p/{profile}" + path, handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    auth = {"Authorization": "Bearer " + keys["alpha"]}
    run_auth = {**auth, "Idempotency-Key": "stable-request"}
    base = "/p/alpha/v1/passive-history"
    run_body = {"session_id": "conversation", "input": "original spoken request",
                "origin": {"event_id": "event", "origin_turn_id": "utterance"}}
    try:
        assert (await client.post("/p/alpha/v1/runs", headers=auth, json=run_body)).status == 400
        response_202 = await client.post("/p/alpha/v1/runs", headers=run_auth, json=run_body)
        assert response_202.status == 202
        accepted = await response_202.json()
        pending = await client.post(base + "/adopt", headers=auth, json=payload)
        assert pending.status == 200
        assert (await pending.json())["status"] == "pending"
        assert constructions == []
        assert db.get_messages("conversation") == prefix
        foreign = await client.post(base.replace("alpha", "beta") + "/adopt", headers=auth, json=payload)
        assert foreign.status == 401
        foreign = await client.post(base.replace("alpha", "beta") + "/adopt",
                                    headers={"Authorization": "Bearer " + keys["beta"]}, json=payload)
        assert (await foreign.json()) == {"profile": "beta", "status": "pending", "reserved": False, "message_ids": []}
        wrong = await client.post(base + "/adopt", headers=auth, json={
            **payload, "messages": [{"role": "user", "content": "different input"}]})
        assert wrong.status == 409
        attached = await client.post(base + "/attach", headers=auth,
                                     json={"session_id": "conversation", "tab_id": "tab"})
        attachment = await attached.json()
        passive = {**payload, **{key: attachment[key] for key in ("tab_id", "attachment_id", "generation")}}
        assert (await client.post(base + "/commit", headers=auth, json=passive)).status == 409
        # A different run cannot claim the same origin, even before the first run writes.
        assert (await client.post("/p/alpha/v1/runs", headers={**auth, "Idempotency-Key": "other-request"},
                                  json=run_body)).status == 409
        release.set()
        await asyncio.wait_for(asyncio.gather(*list(adapter._active_run_tasks.values())), timeout=20)
        proof_response = await client.post(base + "/adopt", headers=auth, json=payload)
        assert proof_response.status == 200
        proof = await proof_response.json()
        assert proof["status"] == "adopted" and proof["run_id"] == accepted["run_id"]
        rows = db.get_messages("conversation")
        assert rows[:len(prefix)] == prefix
        assert [(row["role"], row["content"]) for row in rows[len(prefix):]] == [
            ("user", "original spoken request"), ("assistant", "answer to original request")]
        assert proof["message_ids"] == [rows[-2]["id"]]
        assert provider_saw == ["pending" if failed_first_write else "adopted"]
        assert not hasattr(agent, "_execution_origin_claim")
        assert agent._cached_system_prompt == "cached system prompt\n"
        replay = await client.post("/p/alpha/v1/runs", headers=run_auth, json=run_body)
        assert replay.status == 202 and (await replay.json())["run_id"] == accepted["run_id"]
        assert len(constructions) == 1 and db.get_messages("conversation") == rows
        # A later ordinary request uses the unchanged path and cannot inherit the origin claim.
        agent.client = MagicMock()
        agent.client.chat.completions.create.side_effect = response
        ordinary = await client.post("/p/alpha/v1/runs", headers=auth,
                                     json={"session_id": "conversation", "input": "independent later request"})
        assert ordinary.status == 202
        await asyncio.wait_for(asyncio.gather(*list(adapter._active_run_tasks.values())), timeout=20)
        ordinary_user = db.get_messages("conversation")[-2]
        assert ordinary_user["content"] == "independent later request"
        assert not ordinary_user.get("display_metadata")
        assert len(constructions) == 2 and not hasattr(agent, "_execution_origin_claim")
        db.clear_messages("conversation")
        assert (await client.post(base + "/adopt", headers=auth, json=payload)).status == 410
    finally:
        release.set()
        await client.close()
        await adapter.disconnect()
        for store in stores.values():
            store.close()
