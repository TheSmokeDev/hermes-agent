"""Real discovery and HTTP/canonical-child/control paths, with a non-model plugin worker."""
import asyncio
from unittest.mock import patch

import pytest

from agent import task_worker_registry
from hermes_cli.plugins import PluginManager
from passive_history_ingress import PRODUCER
from run_agent import AIAgent
from tests.gateway.test_dashboard_consumption import gateway

PLUGIN = '''
import threading
from agent.task_worker_provider import TaskWorkerProvider, TaskWorkerSession
class Session(TaskWorkerSession):
    def __init__(self, request):
        self.request=request
        self.finish=threading.Event()
        self.cancelled=False
        self.pending=True
        self.corrections=[]
        self.decisions=[]
    def run(self):
        self.request.report({"status":"waiting_for_approval"})
        self.finish.wait(15)
        return {"status":"cancelled" if self.cancelled else "completed", "output":"Full external result"}
    def cancel(self):
        self.cancelled=True
        self.finish.set()
    def steering(self):
        return {"supported":True,"turn_id":"worker-turn"}
    def steer(self,text,**control):
        self.corrections.append((text,control))
        return "queued"
    def approvals(self):
        return [{"request_id":"approval-one","description":"Fixture approval","choices":["once","deny"]}] if self.pending else []
    def approve(self,request_id,choice):
        if request_id!="approval-one" or not self.pending:
            raise ValueError("No pending approval")
        self.pending=False
        self.decisions.append(choice)
        self.request.report({"status":"running"})
        return {"submitted":True,"request_id":request_id,"evidence":"transport_handoff"}
class Provider(TaskWorkerProvider):
    name="fixture-worker"
    def __init__(self): self.sessions=[]
    def available(self): return True
    def open(self,request):
        session=Session(request)
        self.sessions.append(session)
        return session

def register(ctx):
    ctx.register_task_worker_provider(Provider())
'''


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_worker_discovery_origin_controls_profile_fences_and_terminal_result(tmp_path, monkeypatch, cancel):
    task_worker_registry._reset_for_tests()
    monkeypatch.setattr(AIAgent, "run_conversation", lambda *a, **kw: pytest.fail("parent model ran"))
    monkeypatch.setattr("agent.model_metadata.fetch_model_metadata", lambda *a, **kw: {})
    async with gateway(tmp_path, monkeypatch) as (root, keys, stores, adapter, client):
        home=root/"profiles"/"alpha"
        plugin=home/"plugins"/"fixture-worker"
        plugin.mkdir(parents=True)
        (plugin/"plugin.yaml").write_text("name: fixture-worker\nversion: 1.0.0\ndescription: fixture\n")
        (plugin/"__init__.py").write_text(PLUGIN)
        (home/"config.yaml").write_text("plugins:\n  enabled: [fixture-worker]\n")
        with adapter._profile_scope("alpha"):
            manager=PluginManager()
            manager.discover_and_load()
            provider=task_worker_registry.configured_worker("fixture-worker")
            assert provider is not None
        db=stores["alpha"]
        def parent(**kwargs):
            return AIAgent(api_key="fixture-key",provider="openrouter",model="fixture",
                base_url="https://openrouter.ai/api/v1",session_id=kwargs["session_id"],session_db=db,
                platform="api_server",enabled_toolsets=["file"],quiet_mode=True,skip_context_files=True,
                skip_memory=True,tool_progress_callback=kwargs["tool_progress_callback"])
        monkeypatch.setattr(adapter,"_create_agent",parent)
        auth={"Authorization":"Bearer "+keys["alpha"]}
        beta={"Authorization":"Bearer "+keys["beta"]}
        body={"session_id":"same-session","input":"Original exact instruction",
              "origin":{"event_id":"original-event","origin_turn_id":"original-turn"},
              "child":{"goal":"Derived external goal","correlation_id":"external-action","worker":"fixture-worker"}}
        session=None
        try:
            caps=await (await client.get("/p/alpha/v1/capabilities",headers=auth)).json()
            assert "fixture-worker" in caps["features"]["linked_child_dispatch"]["external_workers"]["names"]
            refused=await client.post("/p/beta/v1/runs",headers={**beta,"Idempotency-Key":"foreign"},json=body)
            assert refused.status==400
            assert stores["beta"].get_messages("same-session")==[]
            with (patch("model_tools.get_tool_definitions",return_value=[]),
                  patch("model_tools.check_toolset_requirements",return_value={}),
                  patch("agent.process_bootstrap.OpenAI")):
                responses=await asyncio.gather(*(client.post("/p/alpha/v1/runs",
                    headers={**auth,"Idempotency-Key":"original-job"},json=body) for _ in range(2)))
                assert all(response.status==202 for response in responses), [await r.text() for r in responses]
                run_ids=[(await response.json())["run_id"] for response in responses]
                assert run_ids[0]==run_ids[1]
                run_id=run_ids[0]
                route=f"/p/alpha/v1/runs/{run_id}"
                async def status_in(states):
                    while True:
                        current=await (await client.get(route,headers=auth)).json()
                        if current.get("status") in states: return current
                        if current.get("status") in {"failed","cancelled","completed"}: raise AssertionError(current)
                        await asyncio.sleep(0.02)
                initial=await asyncio.wait_for(status_in({"waiting_for_approval"}),10)
                assert len(provider.sessions)==1
                session=provider.sessions[0]
                assert session.request.profile=="alpha" and session.request.profile_home==home
                assert session.request.still_authorized()
                assert initial["child_session_id"]==session.request.child_session_id
                assert [row["content"] for row in db.get_messages("same-session")]==[body["input"]]
                target=await (await client.get(route+"/steer",headers=auth)).json()
                assert target["supported"] and target["kind"]=="linked_child"
                text="  Keep this correction\nexactly as spoken  "
                receipt=db.append_passive_messages("same-session",producer=PRODUCER,event_id="correction-event",
                    origin_turn_id="correction-turn",messages=[{"role":"user","content":text}])
                control={"input":text,"control":{"version":1,"action_id":"steer-one",
                    "expected_session_id":target["session_id"],"expected_turn_id":target["turn_id"],
                    "origin":{"event_id":"correction-event","origin_turn_id":"correction-turn","receipt_id":receipt.revision}}}
                first=await (await client.post(route+"/steer",headers=auth,json=control)).json()
                assert first["status"]=="queued",first
                assert await (await client.post(route+"/steer",headers=auth,json=control)).json()==first
                assert len(session.corrections)==1 and session.corrections[0][0]==text
                assert (await client.post(route+"/steer",headers=auth,json={"input":"unlinked"})).status==409
                pending=await (await client.get(route+"/approval",headers=auth)).json()
                assert pending["approvals"][0]["request_id"]=="approval-one"
                assert (await client.get(route.replace("alpha","beta"),headers=beta)).status==404
                accepted=await client.post(route+"/approval",headers=auth,json={"request_id":"approval-one","choice":"once"})
                assert accepted.status==200,await accepted.text()
                assert (await client.post(route+"/approval",headers=auth,json={"request_id":"approval-one","choice":"once"})).status==409
                assert session.decisions==["once"]
                if cancel:
                    assert (await client.post(route+"/stop",headers=auth,json={})).status==200
                else:
                    session.finish.set()
                final=await asyncio.wait_for(status_in({"completed","cancelled"}),10)
                assert final["status"]==("cancelled" if cancel else "completed")
                assert [row["content"] for row in db.get_messages(session.request.child_session_id)]==[
                    body["child"]["goal"],"Full external result"]
                assert [row["content"] for row in db.get_messages("same-session")]==[body["input"],text]
        finally:
            if session: session.finish.set()
            task_worker_registry._reset_for_tests()
