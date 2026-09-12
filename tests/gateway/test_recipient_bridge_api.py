"""Real HTTP routing, session and peer/profile isolation for recipient operations."""
import pytest

from gateway.platforms import api_server_recipient_bridge as api
from tools.computer_use.recipient_bridge import RecipientBridge
from tests.gateway.test_dashboard_consumption import gateway
from tests.tools.test_recipient_bridge import Desktop


@pytest.mark.asyncio
async def test_authenticated_recipient_route_roundtrip_and_scope(tmp_path, monkeypatch):
    desktop = Desktop()
    def factory(**kwargs):
        return RecipientBridge(**kwargs, desktop=desktop)
    monkeypatch.setattr(api, "RecipientBridge", factory)
    async with gateway(tmp_path, monkeypatch) as (_, keys, _, adapter, client):
        auth = {"Authorization": "Bearer " + keys["alpha"]}
        base = "/p/alpha/v1/recipient-bridge/"
        body = {"session_id": "same-session", "actor_scope": "human"}
        assert (await client.post(base + "list", json=body)).status == 401
        missing = await client.post(base + "list", headers=auth, json={**body, "session_id": "missing"})
        assert missing.status == 404
        response = await client.post(base + "list", headers=auth, json=body)
        assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
        target = (await response.json())["recipients"][0]
        payload = {**body, "target_token": target["target_token"], "operation_id": "op", "message": "hello"}
        prepared = await client.post(base + "send", headers=auth, json=payload)
        queued = await prepared.json()
        assert queued["status"] == "queued" and desktop.submits == 0
        posted = await client.post(base + "send", headers=auth, json={**payload, "commit_token": queued["commit_token"]})
        assert (await posted.json())["status"] == "posted" and desktop.submits == 1
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
