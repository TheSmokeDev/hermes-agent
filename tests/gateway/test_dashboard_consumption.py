"""Authenticated live approval reads and same-store proofs for dashboard consumers."""
import asyncio
from contextlib import asynccontextmanager
import secrets
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms import api_server_runs
from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_task_context import resolve_dashboard_task_context
from hermes_state import SessionDB
from hermes_state_store_identity import get_store_id
from starlette.requests import Request
from tools import approval
from tools.approval_gateway_wait import _ApprovalEntry


@asynccontextmanager
async def gateway(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(root))
    keys = {name: secrets.token_hex(24) for name in ("alpha", "beta")}
    stores = {}
    for name, key in keys.items():
        home = root / "profiles" / name
        home.mkdir(parents=True)
        (home / ".env").write_text(f"API_SERVER_KEY={key}\n", encoding="utf-8")
        stores[name] = SessionDB(home / "state.db")
        stores[name].create_session("same-session", source="test")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": keys["alpha"]}))
    adapter.gateway_runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True))
    app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, path, handler)
        app.router.add_route(method, "/p/{profile}" + path, handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield root, keys, stores, adapter, client
    finally:
        await client.close()
        await adapter.disconnect()
        for store in stores.values():
            store.close()


@pytest.mark.asyncio
async def test_approval_reader_uses_live_queue_and_keeps_ownership(tmp_path, monkeypatch):
    ready, release = asyncio.Event(), asyncio.Event()
    records, started = {}, []

    async def pending_run(owner, run, **kwargs):
        started.append(run.run_id)
        callback = lambda data: None
        entry = _ApprovalEntry({"command": "fixture command", "description": "Public prompt",
                                "allow_session": False, "allow_permanent": False})
        approval.register_gateway_notify(run.approval_session_key, callback)
        with approval._lock:
            approval._gateway_queues.setdefault(run.approval_session_key, []).append(entry)
        records[run.run_id] = (entry, callback)
        owner._set_run_status(run.run_id, "waiting_for_approval", approval={"request_id": "stale-cache"})
        ready.set()
        try:
            await release.wait()
        finally:
            approval.unregister_gateway_notify(run.approval_session_key)
            api_server_runs._retire_live_run(owner, run.run_id)

    monkeypatch.setattr(api_server_runs, "_execute_run", pending_run)
    async with gateway(tmp_path, monkeypatch) as (_, keys, stores, adapter, client):
        auth = {"Authorization": "Bearer " + keys["alpha"]}
        created = await client.post("/p/alpha/v1/runs", headers={**auth, "Idempotency-Key": "reader-run"},
                                    json={"session_id": "same-session", "input": "fixture run"})
        assert created.status == 202
        run_id = (await created.json())["run_id"]
        await asyncio.wait_for(ready.wait(), 5)
        entry, callback = records[run_id]
        path = f"/p/alpha/v1/runs/{run_id}/approval"
        try:
            response = await client.get(path, headers=auth)
            payload = await response.json()
            assert response.headers["Cache-Control"] == "no-store"
            assert payload == {"object": "hermes.run.approvals", "run_id": run_id,
                               "status": "waiting_for_approval", "approvals": [dict(entry.data)]}
            assert not entry.event.is_set() and entry.result is None and not entry.acknowledged
            assert approval._gateway_notify_cb(run_id) is callback and started == [run_id]
            assert keys["alpha"] not in str(payload) and "stale-cache" not in str(payload)
            assert (await client.get(path, headers={"Authorization": "Bearer wrong"})).status == 401
            foreign = await client.get(path.replace("alpha", "beta"),
                                       headers={"Authorization": "Bearer " + keys["beta"]})
            assert foreign.status == 404
            resolved = await client.post(path, headers=auth,
                                         json={"choice": "once", "request_id": entry.data["request_id"]})
            assert resolved.status == 200
            assert (await (await client.get(path, headers=auth)).json())["approvals"] == []
            # Revocation and stop cannot resurrect the cached approval metadata.
            second = _ApprovalEntry({"command": "second prompt"})
            with approval._lock:
                approval._gateway_queues.setdefault(run_id, []).append(second)
            approval.unregister_gateway_notify(run_id)
            assert (await (await client.get(path, headers=auth)).json())["approvals"] == []
            third = _ApprovalEntry({"command": "must not be actionable"})
            approval.register_gateway_notify(run_id, callback)
            with approval._lock:
                approval._gateway_queues.setdefault(run_id, []).append(third)
            for phase in ("stopping", "completed"):
                adapter._set_run_status(run_id, phase)
                view = await (await client.get(path, headers=auth)).json()
                assert view["status"] == phase and view["approvals"] == []
                assert not third.event.is_set()  # GET itself did not resolve/revoke anything.
            adapter._set_run_status(run_id, "waiting_for_approval")
            assert (await client.post(f"/p/alpha/v1/runs/{run_id}/stop", headers=auth, json={})).status == 200
            assert (await (await client.get(path, headers=auth)).json())["approvals"] == []
            assert stores["alpha"].get_messages("same-session") == []
        finally:
            release.set()
            await asyncio.gather(*list(adapter._active_run_tasks.values()))


@pytest.mark.asyncio
async def test_store_proof_uses_actual_profile_db_without_unauthorized_exposure(tmp_path, monkeypatch):
    async with gateway(tmp_path, monkeypatch) as (_, keys, stores, adapter, client):
        verified = Session("user", "email", "label", "org", "provider", 9999999999, "access", "refresh")
        request = Request({"type": "http", "method": "POST", "path": "/api/plugin", "headers": [],
                           "state": {"session": verified}})
        local = resolve_dashboard_task_context(request, "alpha")
        alpha = await client.get("/p/alpha/v1/passive-history/capabilities",
                                  headers={"Authorization": "Bearer " + keys["alpha"]})
        remote = await alpha.json()
        assert alpha.headers["Cache-Control"] == "no-store"
        assert remote["store_id"] == local.store_id == get_store_id(stores["alpha"])
        beta = await client.get("/p/beta/v1/passive-history/capabilities",
                                 headers={"Authorization": "Bearer " + keys["beta"]})
        mismatch = await beta.json()
        assert mismatch["store_id"] != local.store_id
        assert str(local.profile_home) not in str(remote) and keys["alpha"] not in str(remote)
        assert (await client.get("/p/alpha/v1/passive-history/capabilities")).status == 401
        adapter._api_key = ""
        public = await (await client.get("/v1/capabilities")).json()
        assert "store_id" not in str(public)
        assert (await client.get("/v1/passive-history/capabilities")).status == 401
        # The mismatch is visible before a consumer sends any attach/commit/work request.
        assert stores["alpha"].get_messages("same-session") == stores["beta"].get_messages("same-session") == []
        assert not adapter._active_run_tasks
