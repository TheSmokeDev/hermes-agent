"""Real API server app -> real attachment store/bytes -> real child dispatch.

Only the model provider is a fixture; every route under test is the one ``connect()``
registers, and every stored byte is written to and read back from a real temporary home.
"""
import asyncio
import base64
import hashlib
import json
import secrets
import time
from contextlib import asynccontextmanager
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import hermes_state_input_attachments as input_attachments
from gateway.config import PlatformConfig
from gateway.platforms import api_server as api_server_module
from gateway.platforms import api_server_input_attachments as attachment_routes
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB
from passive_history_ingress import PRODUCER
from run_agent import AIAgent

ORIGINAL = "Look at the screenshot I just attached and tell me what broke"
GOAL = "Inspect the operator's attachment and report the failure"
ATTACHMENT_TOOLSETS = ["file", "vision"]


def png_bytes(size=(6, 4), color=(190, 40, 40)):
    from PIL import Image
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def upload_body(data, *, upload_id, filename="screenshot.png", content_type="image/png",
                session_id="parent", **extra):
    return {"session_id": session_id, "upload_id": upload_id, "filename": filename,
            "content_type": content_type,
            "content_base64": base64.b64encode(data).decode("ascii"), **extra}


def run_body(references, *, correlation_id="dispatch-one", event_id="speech-event",
             toolsets=None, **child):
    request = {"goal": GOAL, "correlation_id": correlation_id,
               "allowed_toolsets": list(ATTACHMENT_TOOLSETS if toolsets is None else toolsets), **child}
    if references is not None:
        request["attachments"] = references
    return {"session_id": "parent", "input": ORIGINAL,
            "origin": {"event_id": event_id, "origin_turn_id": event_id + "-turn"},
            "child": request}


def reference(receipt, *, sha256=None):
    return {"attachment_id": receipt["attachment_id"], "sha256": sha256 or receipt["sha256"]}


@asynccontextmanager
async def host(tmp_path, monkeypatch, *, name="hermes", middlewares=False, terminal_env="local"):
    """The adapter, its real route table and a real SessionDB over a temporary Hermes home."""
    root = tmp_path / name
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("TERMINAL_ENV", terminal_env)
    db = SessionDB(root / "state.db")
    db.create_session("parent", source="test")
    key = secrets.token_hex(24)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": key}))
    adapter._session_db = db
    if middlewares:
        installed = [mw for mw in (api_server_module.cors_middleware,
                                   api_server_module.body_limit_middleware,
                                   api_server_module.security_headers_middleware) if mw is not None]
        app = web.Application(middlewares=installed, client_max_size=api_server_module.MAX_REQUEST_BYTES)
    else:
        app = web.Application()
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, path, handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield SimpleNamespace(client=client, adapter=adapter, db=db, root=root, key=key,
                              auth={"Authorization": "Bearer " + key})
    finally:
        await client.close()
        await adapter.disconnect()
        db.close()


async def upload(box, data, **kwargs):
    response = await box.client.post("/v1/input-attachments", headers=box.auth,
                                     json=upload_body(data, **kwargs))
    return response, await response.json()


async def dispatch(box, body, *, idempotency_key="one-action"):
    response = await box.client.post(
        "/v1/runs", headers={**box.auth, "Idempotency-Key": idempotency_key}, json=body)
    return response, await response.json()


# --------------------------------------------------------------------------- ingress


@pytest.mark.asyncio
async def test_upload_receipt_is_opaque_and_carries_no_host_path(tmp_path, monkeypatch):
    data = png_bytes()
    async with host(tmp_path, monkeypatch) as box:
        response, receipt = await upload(box, data, upload_id="u1", content_type="text/plain")
        assert response.status == 200
        assert set(receipt) == {"attachment_id", "filename", "content_type", "bytes", "sha256", "state"}
        assert receipt["state"] == "stored"
        assert receipt["filename"] == "screenshot.png"
        # The declared label is not proof of format: the detected type wins.
        assert receipt["content_type"] == "image/png"
        assert receipt["bytes"] == len(data)
        assert receipt["sha256"] == hashlib.sha256(data).hexdigest()
        assert receipt["attachment_id"].startswith("att_") and len(receipt["attachment_id"]) == 36
        rendered = json.dumps(receipt)
        assert str(box.root) not in rendered and box.key not in rendered
        assert response.headers["Cache-Control"] == "no-store"
        # The bytes landed in the images cache root the agent-visible mapping understands.
        stored = box.root / "images" / (receipt["attachment_id"] + ".png")
        assert stored.read_bytes() == data


@pytest.mark.asyncio
async def test_upload_requires_the_gateway_bearer_and_refuses_room_grants(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as box:
        body = upload_body(png_bytes(), upload_id="u1")
        assert (await box.client.post("/v1/input-attachments", json=body)).status == 401
        wrong = await box.client.post("/v1/input-attachments", json=body,
                                      headers={"Authorization": "Bearer nope"})
        assert wrong.status == 401
        room = await box.client.post("/v1/input-attachments", json=body,
                                     headers={"Authorization": "HermesRoom " + box.key})
        assert room.status == 403
        assert (await room.json())["error"] == "attachment_room_grant_unsupported"
        # Nothing was stored for any refused caller.
        assert not list((box.root / "images").glob("*")) if (box.root / "images").exists() else True


@pytest.mark.asyncio
async def test_repeated_upload_id_replays_identical_bytes_and_conflicts_on_change(tmp_path, monkeypatch):
    first_bytes, other_bytes = png_bytes(), png_bytes(size=(8, 8), color=(10, 90, 200))
    async with host(tmp_path, monkeypatch) as box:
        _, first = await upload(box, first_bytes, upload_id="u1")
        _, replay = await upload(box, first_bytes, upload_id="u1")
        assert replay == first
        changed, payload = await upload(box, other_bytes, upload_id="u1")
        assert changed.status == 409 and payload["error"] == "attachment_upload_conflict"
        renamed, payload = await upload(box, first_bytes, upload_id="u1", filename="other.png")
        assert renamed.status == 409 and payload["error"] == "attachment_upload_conflict"
        # Identical bytes under a different upload id are a separate receipt.
        _, sibling = await upload(box, first_bytes, upload_id="u2")
        assert sibling["attachment_id"] != first["attachment_id"]
        assert sibling["sha256"] == first["sha256"]


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", [
    "../escape.png", "..\\escape.png", "dir/shot.png", "C:\\secrets\\shot.png",
    "CON.png", "COM1.txt", "NUL", "shot\x00.png", "shot\n.png", " shot.png", "shot.", "."])
async def test_hostile_filenames_are_rejected_as_display_metadata(tmp_path, monkeypatch, filename):
    async with host(tmp_path, monkeypatch) as box:
        response, payload = await upload(box, png_bytes(), upload_id="u1", filename=filename)
        assert response.status == 400, (filename, payload)
        assert payload["error"] == "invalid_attachment_filename"


@pytest.mark.asyncio
async def test_image_payloads_are_validated_from_bytes_not_labels(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as box:
        lying, payload = await upload(box, b"this is not a png", upload_id="u1",
                                      filename="notes.txt", content_type="image/png")
        assert lying.status == 400 and payload["error"] == "invalid_attachment_image"
        by_suffix, payload = await upload(box, b"this is not a png", upload_id="u2",
                                          filename="shot.png", content_type="text/plain")
        assert by_suffix.status == 400 and payload["error"] == "invalid_attachment_image"
        # A genuine non-image file stores as an opaque binary, never as a claimed format.
        ok, receipt = await upload(box, b"plain notes\n", upload_id="u3",
                                   filename="notes.txt", content_type="text/plain")
        assert ok.status == 200 and receipt["content_type"] == "application/octet-stream"
        assert (box.root / "attachments" / (receipt["attachment_id"] + ".txt")).exists()
        empty, payload = await upload(box, b"", upload_id="u4")
        assert empty.status == 413 and payload["error"] == "attachment_size_limit"
        broken = await box.client.post("/v1/input-attachments", headers=box.auth, json={
            **upload_body(b"x", upload_id="u5"), "content_base64": "!!!not base64!!!"})
        assert broken.status == 400 and (await broken.json())["error"] == "invalid_attachment_base64"


@pytest.mark.asyncio
async def test_per_file_and_body_bounds_are_enforced_by_the_real_handler(tmp_path, monkeypatch):
    monkeypatch.setattr(input_attachments, "MAX_FILE_BYTES", 256)
    async with host(tmp_path, monkeypatch) as box:
        big, payload = await upload(box, b"x" * 512, upload_id="u1", filename="big.bin",
                                    content_type="application/octet-stream")
        assert big.status == 413 and payload["error"] == "attachment_size_limit"
        monkeypatch.setattr(input_attachments, "MAX_UPLOAD_BODY_BYTES", 64)
        over, payload = await upload(box, b"x" * 100, upload_id="u2", filename="big.bin",
                                     content_type="application/octet-stream")
        assert over.status == 413 and payload["error"] == "attachment_body_too_large"


@pytest.mark.asyncio
async def test_only_the_attachment_route_gets_the_larger_request_body_limit(tmp_path, monkeypatch):
    """Ordinary API request limits are unchanged; the ingress declares its own explicit bound."""
    monkeypatch.setattr(api_server_module, "MAX_REQUEST_BYTES", 2048)
    data = secrets.token_bytes(4096)
    async with host(tmp_path, monkeypatch, middlewares=True) as box:
        kwargs = {"filename": "blob.bin", "content_type": "application/octet-stream"}
        assert len(json.dumps(upload_body(data, upload_id="u1", **kwargs))) > 2048
        response, receipt = await upload(box, data, upload_id="u1", **kwargs)
        assert response.status == 200 and receipt["bytes"] == len(data)
        oversized = await box.client.post(
            "/v1/runs", headers={**box.auth, "Idempotency-Key": "k"},
            json={"session_id": "parent", "input": "p" * 4096})
        assert oversized.status == 413
        assert (await oversized.json())["error"]["code"] == "body_too_large"


# --------------------------------------------------------------------------- admission


@pytest.mark.asyncio
async def test_duplicate_and_overlong_reference_lists_are_refused(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as box:
        _, receipt = await upload(box, png_bytes(), upload_id="u1")
        duplicated, payload = await dispatch(box, run_body([reference(receipt), reference(receipt)]))
        assert duplicated.status == 400
        assert payload["error"]["code"] == "invalid_child_dispatch"
        too_many = [{"attachment_id": "att_" + f"{n:032x}", "sha256": "a" * 64} for n in range(9)]
        overlong, payload = await dispatch(box, run_body(too_many), idempotency_key="k2")
        assert overlong.status == 400 and payload["error"]["code"] == "invalid_child_dispatch"
        malformed, payload = await dispatch(
            box, run_body([{"attachment_id": "not-an-id", "sha256": "a" * 64}]), idempotency_key="k3")
        assert malformed.status == 400 and payload["error"]["code"] == "invalid_child_dispatch"


@pytest.mark.asyncio
async def test_a_reference_owned_by_another_credential_is_refused(tmp_path, monkeypatch):
    """The second listener authenticates with its own key, so its derived owner scope differs."""
    data = png_bytes()
    async with host(tmp_path, monkeypatch) as box:
        foreign_key = secrets.token_hex(24)
        foreign = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": foreign_key}))
        foreign._session_db = box.db
        foreign_app = web.Application()
        for method, path, handler in foreign._http_route_table():
            foreign_app.router.add_route(method, path, handler)
        peer = TestClient(TestServer(foreign_app))
        await peer.start_server()
        try:
            response = await peer.post(
                "/v1/input-attachments",
                headers={"Authorization": "Bearer " + foreign_key},
                json=upload_body(data, upload_id="u1"))
            assert response.status == 200
            stolen = reference(await response.json())
        finally:
            await peer.close()
            await foreign.disconnect()
        refused, payload = await dispatch(box, run_body([stolen]))
        assert refused.status == 404 and payload["error"] == "attachment_not_found"
        assert box.db.get_messages("parent") == []


@pytest.mark.asyncio
async def test_a_reference_with_a_wrong_hash_is_refused(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as box:
        _, receipt = await upload(box, png_bytes(), upload_id="u1")
        refused, payload = await dispatch(
            box, run_body([reference(receipt, sha256="b" * 64)]))
        assert refused.status == 409 and payload["error"] == "attachment_hash_conflict"
        assert box.db.get_messages("parent") == []


@pytest.mark.asyncio
async def test_expired_unbound_uploads_cannot_be_dispatched(tmp_path, monkeypatch):
    monkeypatch.setattr(input_attachments, "UNBOUND_TTL_SECONDS", -1)
    async with host(tmp_path, monkeypatch) as box:
        _, stale = await upload(box, png_bytes(), upload_id="u1")
        stored = box.root / "images" / (stale["attachment_id"] + ".png")
        assert stored.exists()
        # A later upload runs the retention sweep: the unbound bytes go, the id is tombstoned.
        monkeypatch.setattr(input_attachments, "UNBOUND_TTL_SECONDS", 3600)
        await upload(box, png_bytes(size=(5, 5)), upload_id="u2")
        assert not stored.exists()
        refused, payload = await dispatch(box, run_body([reference(stale)]))
        assert refused.status == 410 and payload["error"] == "attachment_expired"


@pytest.mark.asyncio
async def test_changed_audience_invalidates_a_bound_reference(tmp_path, monkeypatch):
    """Store + canonical dispatch are real; only the verified audience snapshot is supplied."""
    async with host(tmp_path, monkeypatch) as box:
        store = input_attachments.InputAttachmentStore(box.db)
        scope = hashlib.sha256(b"owner").hexdigest()
        audience = input_attachments.audience_key({
            "surface": "discord", "profile": "default", "guild_id": "1", "channel_id": "2",
            "operator_user_id": "3", "audience_revision": "rev-1", "audience_user_ids": ["3"]})
        receipt = store.store(upload_body(png_bytes(), upload_id="u1"),
                              owner_scope=scope, audience=audience)
        moved = input_attachments.audience_key({
            "surface": "discord", "profile": "default", "guild_id": "1", "channel_id": "2",
            "operator_user_id": "3", "audience_revision": "rev-2", "audience_user_ids": ["3", "4"]})
        with pytest.raises(Exception) as exc:
            box.db.prepare_child_dispatch(
                "parent", producer=PRODUCER, event_id="e1", origin_turn_id="t1", content=ORIGINAL,
                run_id="run_a", run_scope=scope, correlation_id="c1", fingerprint="f1",
                attachments=[reference(receipt)], attachment_audience=moved)
        assert getattr(exc.value, "code", "") == "attachment_audience_changed"
        # The same reference under its own audience is accepted.
        dispatched = box.db.prepare_child_dispatch(
            "parent", producer=PRODUCER, event_id="e1", origin_turn_id="t1", content=ORIGINAL,
            run_id="run_a", run_scope=scope, correlation_id="c1", fingerprint="f1",
            attachments=[reference(receipt)], attachment_audience=audience)
        assert dispatched["run_id"] == "run_a"


@pytest.mark.asyncio
async def test_a_malformed_discord_binding_on_upload_reaches_the_real_verifier(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as box:
        response = await box.client.post("/v1/input-attachments", headers=box.auth, json={
            **upload_body(png_bytes(), upload_id="u1"),
            "discord_task_context": {"proof": "p", "session_id": "other", "binding_id": "b"}})
        assert response.status == 400
        assert (await response.json())["error"] == "invalid_discord_binding"
        assert not list((box.root / "images").glob("*")) if (box.root / "images").exists() else True


@pytest.mark.asyncio
async def test_installed_workers_and_narrow_toolsets_refuse_attachment_delivery(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as box:
        _, receipt = await upload(box, png_bytes(), upload_id="u1")
        narrow, payload = await dispatch(box, run_body([reference(receipt)], toolsets=["file"]))
        assert narrow.status == 400
        assert "child_toolsets_missing_attachment_tools" in payload["error"]["message"]
        worker_body = run_body([reference(receipt)])
        worker_body["child"].pop("allowed_toolsets")
        worker_body["child"]["worker"] = "codex"
        worker, payload = await dispatch(box, worker_body, idempotency_key="k2")
        assert worker.status == 400
        assert "worker_attachment_delivery_unsupported" in payload["error"]["message"]
        monkeypatch.setenv("TERMINAL_ENV", "modal")
        unverified, payload = await dispatch(box, run_body([reference(receipt)]), idempotency_key="k3")
        assert unverified.status == 400
        assert "terminal_backend_delivery_unverified" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_capabilities_publish_the_exact_ingress_limits_and_unsupported_reasons(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as box:
        response = await box.client.get("/v1/capabilities", headers=box.auth)
        assert response.status == 200
        payload = await response.json()
        feature = payload["features"]["input_attachments"]
        assert feature["version"] == 1 and feature["supported"] is True
        assert feature["endpoint"] == {"method": "POST", "path": "/v1/input-attachments"}
        assert feature["dispatch_field"] == "child.attachments"
        assert feature["reference_fields"] == ["attachment_id", "sha256"]
        assert feature["limits"] == {
            "max_files_per_input": 8, "max_file_bytes": 10 * 1024 * 1024,
            "max_input_bytes": 20 * 1024 * 1024, "max_upload_body_bytes": 13_989_208}
        assert feature["retention"] == {"unbound_ttl_seconds": 86400, "bound_attachments_retained": True}
        assert feature["inspection_requires_tool_use"] is True
        child = feature["delivery"]["hermes_child"]
        assert child["available"] is True and child["terminal_backend"] == "local"
        assert child["required_toolset_tools"] == ["read_file", "vision_analyze"]
        for unsupported in ("external_task_workers", "existing_app_recipients"):
            assert feature["delivery"][unsupported]["available"] is False
            assert feature["delivery"][unsupported]["reason"]
        assert payload["endpoints"]["input_attachment_upload"] == {
            "method": "POST", "path": "/v1/input-attachments"}
        rendered = json.dumps(payload)
        assert str(box.root) not in rendered and box.key not in rendered


@pytest.mark.asyncio
async def test_an_unverified_terminal_backend_is_advertised_unavailable(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch, terminal_env="ssh") as box:
        payload = await (await box.client.get("/v1/capabilities", headers=box.auth)).json()
        child = payload["features"]["input_attachments"]["delivery"]["hermes_child"]
        assert child["available"] is False
        assert child["reason"] == "terminal_backend_delivery_unverified"


# --------------------------------------------------------------------------- dispatch


def child_only_provider(monkeypatch, payloads, children):
    """Run the real child agent/loop; the provider answers from a fixture."""
    from tests.run_agent.test_run_agent import _mock_response
    original_run = AIAgent.run_conversation

    def child_only(self, *args, **kwargs):
        assert self.platform == "subagent", "parent model execution is forbidden"
        children.append(self)
        self.compression_enabled = False
        self.save_trajectories = False
        self.tool_delay = 0
        self.client = MagicMock()

        def provider_response(**request):
            payloads.append(json.dumps(request["messages"]))
            return _mock_response(content="Child result", finish_reason="stop")

        self.client.chat.completions.create.side_effect = provider_response
        return original_run(self, *args, **kwargs)

    monkeypatch.setattr(AIAgent, "run_conversation", child_only)
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *a, **k: None)
    monkeypatch.setattr("tools.delegate_tool._get_worktree_isolation", lambda: False)


def parent_builder(adapter, db, monkeypatch, parents):
    def build_parent(**kwargs):
        parent = AIAgent(api_key="fixture-only-key", provider="openrouter", model="test-model",
                         base_url="https://openrouter.ai/api/v1", session_id=kwargs["session_id"],
                         session_db=db, platform="api_server", enabled_toolsets=ATTACHMENT_TOOLSETS,
                         quiet_mode=True, skip_context_files=True, skip_memory=True,
                         tool_progress_callback=kwargs["tool_progress_callback"])
        parents.append(parent)
        return parent
    monkeypatch.setattr(adapter, "_create_agent", build_parent)


def record_child_context(monkeypatch, contexts):
    """Capture the context the real lifecycle service is launched with."""
    from agent import subagent_lifecycle
    real_launch = subagent_lifecycle.SubagentLifecycleService.launch

    def record(service, request):
        contexts.append(request.context)
        return real_launch(service, request)

    monkeypatch.setattr(subagent_lifecycle.SubagentLifecycleService, "launch", record)


def parse_manifest(context):
    head, separator, body = context.partition(attachment_routes.MANIFEST_HEADING + "\n")
    assert separator, context
    return json.loads(body)


async def wait_terminal(box, run_id):
    async def wait():
        while True:
            status = await (await box.client.get(f"/v1/runs/{run_id}", headers=box.auth)).json()
            if status.get("status") in {"completed", "failed", "cancelled"}:
                return status
            await asyncio.sleep(0.02)
    return await asyncio.wait_for(wait(), timeout=30)


@pytest.mark.asyncio
async def test_dispatch_freezes_the_set_and_the_child_context_carries_readable_mapped_paths(
        tmp_path, monkeypatch):
    image, document = png_bytes(size=(9, 7), color=(3, 140, 90)), b"failing stack trace\n"
    payloads, children, parents, contexts = [], [], [], []
    child_only_provider(monkeypatch, payloads, children)
    async with host(tmp_path, monkeypatch) as box:
        parent_builder(box.adapter, box.db, monkeypatch, parents)
        record_child_context(monkeypatch, contexts)
        _, shot = await upload(box, image, upload_id="u1")
        _, notes = await upload(box, document, upload_id="u2", filename="trace.log",
                                content_type="text/plain")
        references = [reference(notes), reference(shot)]  # deliberately not id-sorted
        body = run_body(references)
        with (patch("model_tools.get_tool_definitions", return_value=[]),
              patch("model_tools.check_toolset_requirements", return_value={}),
              patch("agent.process_bootstrap.OpenAI")):
            response, admitted = await dispatch(box, body)
            assert response.status == 202, admitted
            terminal = await wait_terminal(box, admitted["run_id"])
            assert terminal["status"] == "completed", terminal
            assert len(children) == 1
            # The manifest the server built reached the child's real launch context AND the model.
            assert len(contexts) == 1
            manifest = parse_manifest(contexts[0])
            assert attachment_routes.MANIFEST_HEADING in json.dumps(payloads)
            assert manifest["attachments"][0]["attachment_id"] in json.dumps(payloads)
            assert manifest["version"] == 1
            assert [item["attachment_id"] for item in manifest["attachments"]] == sorted(
                item["attachment_id"] for item in manifest["attachments"])
            by_name = {item["filename"]: item for item in manifest["attachments"]}
            assert set(by_name) == {"screenshot.png", "trace.log"}
            assert by_name["screenshot.png"]["kind"] == "image"
            assert by_name["screenshot.png"]["content_type"] == "image/png"
            assert by_name["trace.log"]["kind"] == "file"
            assert by_name["trace.log"]["content_type"] == "application/octet-stream"
            # The mapped paths are what a file/vision tool would actually open.
            from pathlib import Path
            assert Path(by_name["screenshot.png"]["path"]).read_bytes() == image
            assert Path(by_name["trace.log"]["path"]).read_bytes() == document
            assert by_name["screenshot.png"]["sha256"] == hashlib.sha256(image).hexdigest()
            assert "read" in manifest["guidance"].lower() or "open" in manifest["guidance"].lower()
            assert "vision_analyze" in manifest["guidance"]
            # The original operator text is unchanged and carries no attachment marker.
            rows = box.db.get_messages("parent")
            assert [(row["role"], row["content"]) for row in rows] == [("user", ORIGINAL)]
            # The frozen set is immutable: the same action with different references conflicts.
            swapped = run_body([reference(shot)])
            conflict, payload = await dispatch(box, swapped, idempotency_key="k2")
            assert conflict.status == 409, payload
            # And a fresh action cannot re-present a reference already bound to this input.
            reused = run_body([reference(shot)], correlation_id="dispatch-two", event_id="speech-two")
            refused, payload = await dispatch(box, reused, idempotency_key="k3")
            assert refused.status == 409 and payload["error"] == "attachment_already_bound"
            assert len(children) == 1


@pytest.mark.asyncio
async def test_retention_never_reclaims_bytes_bound_to_durable_accepted_work(tmp_path, monkeypatch):
    image, orphan = png_bytes(size=(11, 5), color=(60, 60, 200)), png_bytes(size=(3, 3))
    payloads, children, parents = [], [], []
    child_only_provider(monkeypatch, payloads, children)
    async with host(tmp_path, monkeypatch) as box:
        parent_builder(box.adapter, box.db, monkeypatch, parents)
        _, bound = await upload(box, image, upload_id="u1")
        _, unbound = await upload(box, orphan, upload_id="u2", filename="stray.png")
        with (patch("model_tools.get_tool_definitions", return_value=[]),
              patch("model_tools.check_toolset_requirements", return_value={}),
              patch("agent.process_bootstrap.OpenAI")):
            response, admitted = await dispatch(box, run_body([reference(bound)]))
            assert response.status == 202, admitted
            assert (await wait_terminal(box, admitted["run_id"]))["status"] == "completed"
        kept = box.root / "images" / (bound["attachment_id"] + ".png")
        stray = box.root / "images" / (unbound["attachment_id"] + ".png")
        assert kept.exists() and stray.exists()
        # Sweep far past the unbound TTL: only work nothing durable owns is reclaimed.
        store = input_attachments.InputAttachmentStore(box.db)
        removed = store.expire_unbound(now=time.time() + 10 * input_attachments.UNBOUND_TTL_SECONDS)
        assert removed == 1
        assert kept.read_bytes() == image
        assert not stray.exists()
        # The tombstoned id stays refused; the bound one is still a verified reference.
        refused, payload = await dispatch(
            box, run_body([reference(unbound)], correlation_id="d2", event_id="speech-two"),
            idempotency_key="k2")
        assert refused.status == 410 and payload["error"] == "attachment_expired"
        with box.db._read_ctx() as conn:
            row = conn.execute("SELECT expired FROM input_attachments WHERE attachment_id=?",
                               (bound["attachment_id"],)).fetchone()
        assert row["expired"] == 0


@pytest.mark.asyncio
async def test_a_text_only_dispatch_is_byte_identical_to_the_callers_context(tmp_path, monkeypatch):
    payloads, children, parents = [], [], []
    child_only_provider(monkeypatch, payloads, children)
    contexts = []
    async with host(tmp_path, monkeypatch) as box:
        parent_builder(box.adapter, box.db, monkeypatch, parents)
        record_child_context(monkeypatch, contexts)
        body = run_body(None)
        body["child"]["context"] = "Bounded child context"
        with (patch("model_tools.get_tool_definitions", return_value=[]),
              patch("model_tools.check_toolset_requirements", return_value={}),
              patch("agent.process_bootstrap.OpenAI")):
            response, admitted = await dispatch(box, body)
            assert response.status == 202, admitted
            terminal = await wait_terminal(box, admitted["run_id"])
            assert terminal["status"] == "completed", terminal
        assert contexts == ["Bounded child context"]
        assert attachment_routes.MANIFEST_HEADING not in json.dumps(payloads)


@pytest.mark.asyncio
async def test_the_total_input_byte_bound_is_enforced_across_references(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as box:
        _, first = await upload(box, b"a" * 400, upload_id="u1", filename="a.bin",
                                content_type="application/octet-stream")
        _, second = await upload(box, b"b" * 400, upload_id="u2", filename="b.bin",
                                 content_type="application/octet-stream")
        monkeypatch.setattr(input_attachments, "MAX_INPUT_BYTES", 500)
        refused, payload = await dispatch(box, run_body([reference(first), reference(second)]))
        assert refused.status == 413 and payload["error"] == "attachment_input_size_limit"
        assert box.db.get_messages("parent") == []


@pytest.mark.asyncio
async def test_the_profile_mirror_keeps_each_profiles_bytes_in_its_own_store(tmp_path, monkeypatch):
    """/p/{profile}/v1/input-attachments is served, and two profiles never share a receipt."""
    from tests.gateway.test_dashboard_consumption import gateway
    monkeypatch.setenv("TERMINAL_ENV", "local")
    data = png_bytes(size=(7, 7), color=(120, 15, 180))
    async with gateway(tmp_path, monkeypatch) as (root, keys, _, _, client):
        receipts = {}
        for name in ("alpha", "beta"):
            response = await client.post(
                f"/p/{name}/v1/input-attachments",
                headers={"Authorization": "Bearer " + keys[name]},
                json=upload_body(data, upload_id="shared-upload-id", session_id="same-session"))
            assert response.status == 200, await response.text()
            receipts[name] = await response.json()
        # Identical bytes, identical upload id, two profiles: two independent receipts.
        assert receipts["alpha"]["attachment_id"] != receipts["beta"]["attachment_id"]
        assert receipts["alpha"]["sha256"] == receipts["beta"]["sha256"]
        for name, receipt in receipts.items():
            home = root / "profiles" / name
            assert (home / "images" / (receipt["attachment_id"] + ".png")).read_bytes() == data
            other = "beta" if name == "alpha" else "alpha"
            assert not (home / "images" / (receipts[other]["attachment_id"] + ".png")).exists()
        # A profile's bearer token is refused on another profile's mirror.
        crossed = await client.post(
            "/p/beta/v1/input-attachments", headers={"Authorization": "Bearer " + keys["alpha"]},
            json=upload_body(data, upload_id="u2", session_id="same-session"))
        assert crossed.status == 401


@pytest.mark.asyncio
async def test_no_response_ever_renders_a_private_path_or_the_gateway_key(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as box:
        _, receipt = await upload(box, png_bytes(), upload_id="u1")
        rendered = []
        for response in (
            await box.client.post("/v1/input-attachments", headers=box.auth,
                                  json=upload_body(png_bytes(), upload_id="u1",
                                                   filename="../escape.png")),
            await box.client.post("/v1/input-attachments", headers=box.auth,
                                  json=upload_body(b"not a png", upload_id="u2",
                                                   content_type="image/png")),
            await box.client.post("/v1/runs", headers={**box.auth, "Idempotency-Key": "k1"},
                                  json=run_body([reference(receipt, sha256="c" * 64)])),
            await box.client.get("/v1/capabilities", headers=box.auth),
        ):
            rendered.append(await response.text())
        blob = "\n".join(rendered)
        for secret in (box.key, str(box.root), str(box.root / "state.db"), "state.db", "images/att_"):
            assert secret not in blob, secret
