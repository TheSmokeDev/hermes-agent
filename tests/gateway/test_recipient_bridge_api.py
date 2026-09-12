"""Real HTTP routing, session and peer/profile isolation for recipient operations."""
import json
from types import SimpleNamespace

import pytest

from gateway.platforms import api_server_recipient_bridge as api
from tools.computer_use.recipient_bridge import RecipientBridge
from tests.gateway.test_dashboard_consumption import gateway
from tests.tools.test_recipient_bridge import Desktop
from tests.tools.test_recipient_claude import peer as peer


@pytest.mark.asyncio
async def test_authenticated_recipient_route_roundtrip_and_scope(tmp_path, tmp_path_factory, monkeypatch):
    desktop = Desktop()
    state_dir = tmp_path_factory.mktemp("rb")
    def factory(**kwargs):
        return RecipientBridge(**kwargs, state_dir=state_dir, desktop=desktop,
                               claude=SimpleNamespace(list_recipients=lambda: []))
    monkeypatch.setattr(api, "RecipientBridge", factory)
    async with gateway(tmp_path, monkeypatch) as (_, keys, _, adapter, client):
        auth = {"Authorization": "Bearer " + keys["alpha"]}
        base = "/p/alpha/v1/recipient-bridge/"
        body = {"session_id": "same-session", "actor_scope": "human"}
        assert (await client.post(base + "list", json=body)).status == 401
        missing = await client.post(base + "list", headers=auth, json={**body, "session_id": "missing"})
        assert missing.status == 404
        response = await client.post(base + "list", headers=auth, json={**body, "app": "codex_desktop"})
        assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
        catalog = await response.json()
        target = catalog["recipients"][0]
        computer = catalog["capabilities"]["computer_use"]
        assert computer["mode"] == "delegated" and computer["verified"] is True
        assert computer["tool"] == "inspect_screen" and target["app"] == "codex_desktop"
        proof = await client.post(base + "select", headers=auth,
                                  json={**body, "target_token": target["target_token"]})
        assert (await proof.json())["status"] == "selected"
        payload = {**body, "target_token": target["target_token"], "operation_id": "op", "message": "hello"}
        prepared = await client.post(base + "send", headers=auth, json=payload)
        queued = await prepared.json()
        assert queued["status"] == "queued" and desktop.submits == 0
        posted = await client.post(base + "send", headers=auth, json={**payload, "commit_token": queued["commit_token"]})
        posted_receipt = await posted.json()
        assert posted_receipt["status"] == "posted" and desktop.submits == 1
        reconciled = await client.post(base + "reconcile", headers=auth,
                                       json={**body, "target_token": target["target_token"], "operation_id": "op"})
        assert await reconciled.json() == posted_receipt
        captured = await client.post(base + "inspect", headers=auth,
                                    json={**body, "target_token": target["target_token"], "capture": True})
        capture = await captured.json()
        assert captured.status == 200 and capture["status"] == "completed"
        assert capture["artifact_id"] == capture["artifact"]["artifact_id"]
        assert capture["captured_at"] == capture["artifact"]["captured_at"]
        assert capture["artifact"]["mime_type"] == "image/png"
        for receipt in (queued, posted_receipt, capture):
            assert all(receipt[k] == target[k] for k in ("recipient_id", "app", "task_id"))
        foreign = await client.post(base + "select", headers=auth,
                                    json={**body, "actor_scope": "other", "target_token": target["target_token"]})
        assert foreign.status == 404
        foreign_profile = await client.post("/p/beta/v1/recipient-bridge/select",
                                            headers={"Authorization": "Bearer " + keys["beta"]},
                                            json={**body, "target_token": target["target_token"]})
        assert foreign_profile.status == 404
        descriptor = await client.get("/p/alpha/v1/capabilities", headers=auth)
        assert (await descriptor.json())["features"]["recipient_bridge"]["two_phase_send"] is True


@pytest.mark.asyncio
async def test_explicit_discord_proof_failure_blocks_native_discovery(tmp_path, monkeypatch):
    native = []
    monkeypatch.setattr(api, "RecipientBridge", lambda **kwargs: native.append(kwargs))
    async with gateway(tmp_path, monkeypatch) as (_, keys, _, _, client):
        response = await client.post("/p/alpha/v1/recipient-bridge/list",
                                     headers={"Authorization": "Bearer " + keys["alpha"]},
                                     json={"session_id": "same-session", "actor_scope": "human",
                                           "discord_binding": {"proof": "forged", "session_id": "wrong",
                                                               "binding_id": "binding"}})
        assert response.status == 400 and native == []


@pytest.mark.asyncio
async def test_claude_peer_two_phase_route_and_exact_reconciliation(tmp_path, monkeypatch, peer):
    claude, native, _, _, _, history = peer
    desktop = Desktop()
    monkeypatch.setattr(api, "RecipientBridge", lambda **kw: RecipientBridge(**kw, desktop=desktop, claude=claude))
    async with gateway(tmp_path, monkeypatch) as (_, keys, _, _, client):
        auth = {"Authorization": "Bearer " + keys["alpha"]}
        base = "/p/alpha/v1/recipient-bridge/"
        body = {"session_id": "same-session", "actor_scope": "human"}
        response = await client.post(base + "list", headers=auth, json={**body, "app": "claude_code"})
        target = (await response.json())["recipients"][0]
        assert target["proven_control"] == "peer_ipc" and target["app"] == "claude_code"
        payload = {**body, "target_token": target["target_token"], "operation_id": "op", "message": "continue existing"}
        response = await client.post(base + "send", headers=auth, json=payload)
        queued = await response.json()
        assert queued["status"] == "queued" and not native.writes and native.secrets_read == 0
        attempt = {**payload, "commit_token": queued["commit_token"]}
        response = await client.post(base + "send", headers=auth, json=attempt)
        assert (await response.json())["status"] == "unknown" and len(native.writes) == 1
        response = await client.post(base + "send", headers=auth, json=attempt)
        assert (await response.json())["status"] == "unknown" and len(native.writes) == 1
        wire = json.loads(native.writes[0].splitlines()[1])
        history.write_text(json.dumps({"type": "user", "sessionId": target["task_id"], "uuid": wire["uuid"],
                                      "origin": {"kind": "peer", "msg_id": wire["msg_id"]},
                                      "message": {"role": "user", "content": payload["message"]}}) + "\n", encoding="utf-8")
        response = await client.post(base + "reconcile", headers=auth,
                                    json={**body, "target_token": target["target_token"], "operation_id": "op"})
        posted = await response.json()
        assert posted["status"] == "posted" and posted["message_receipt"]["native_message_id"] == wire["msg_id"]
        assert all(posted[k] == target[k] for k in ("recipient_id", "app", "task_id"))
        assert len(native.writes) == 1 and desktop.composes == desktop.submits == 0


@pytest.mark.asyncio
async def test_revoked_audience_cannot_receive_private_native_result(tmp_path, monkeypatch):
    from tools.computer_use.recipient_contract import RecipientError
    revoked = [False]
    class NativeRead:
        def __init__(self, **kwargs):
            pass
        def list_recipients(self):
            revoked[0] = True
            return {"recipients": [{"title": "private task result"}]}
    def verify(*args, **kwargs):
        if revoked[0]:
            raise RecipientError("recipient_authority_revoked", 403)
    monkeypatch.setattr(api, "RecipientBridge", NativeRead)
    monkeypatch.setattr(api, "verify_request", verify)
    async with gateway(tmp_path, monkeypatch) as (_, keys, _, _, client):
        response = await client.post("/p/alpha/v1/recipient-bridge/list",
                                     headers={"Authorization": "Bearer " + keys["alpha"]},
                                     json={"session_id": "same-session", "actor_scope": "human"})
        assert response.status == 403
        assert await response.json() == {"error": "recipient_authority_revoked"}
