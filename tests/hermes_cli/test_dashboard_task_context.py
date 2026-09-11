"""Verified dashboard identity, shared-role continuity and real profile resolution."""
from dataclasses import FrozenInstanceError, asdict, replace
import json
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
import pytest

from hermes_cli.dashboard_auth.base import Session, TokenPrincipal
from hermes_cli.dashboard_task_context import resolve_dashboard_task_context


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    (root / "profiles" / "alpha").mkdir(parents=True)
    (root / "profiles" / "beta").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    from hermes_cli import web_server
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", "current-dashboard-token")
    app = FastAPI()
    app.state.auth_required = False
    return root, app, web_server


def request(app, **state):
    req = Request({"type": "http", "method": "POST", "path": "/api/plugin/task", "app": app,
                   "headers": [], "client": ("127.0.0.1", 12345), "state": state})
    return req


def session(**overrides):
    return Session(**{
        "provider": "oauth", "org_id": "org-one", "user_id": "user-one",
        "email": "private@example.invalid", "display_name": "Private operator",
        "expires_at": 9999999999, "access_token": "secret-access", "refresh_token": "secret-refresh",
        **overrides})


def with_token(req, server, value="current-dashboard-token", bearer=False):
    req.scope["headers"] = [(b"authorization", ("Bearer " + value).encode())] if bearer else [
        (server._SESSION_HEADER_NAME.lower().encode(), value.encode())]
    return req


@pytest.mark.parametrize("provider", ["oauth", "password", "native"])
def test_verified_identity_is_stable_secret_free_and_frozen(dashboard, provider):
    root, app, _ = dashboard
    app.state.auth_required = True
    verified = session(provider=provider)
    context = resolve_dashboard_task_context(request(app, session=verified), " alpha ")
    rotated = replace(verified, access_token="rotated-access", refresh_token="rotated-refresh",
                      email="changed@example.invalid", display_name="Changed label", expires_at=9999999998)
    assert resolve_dashboard_task_context(request(app, session=rotated), "alpha") == context
    assert context.principal_kind == "verified_session"
    assert context.profile_name == "alpha" and context.profile_home == root / "profiles" / "alpha"
    assert str(root) not in repr(context)
    serialized = json.dumps(asdict(context), default=str)
    for secret in (verified.access_token, verified.refresh_token, verified.email, verified.display_name):
        assert secret not in repr(context) and secret not in serialized
    with pytest.raises(FrozenInstanceError):
        context.principal_id = "forged"
    with pytest.raises(TypeError):
        json.dumps(context)  # no implicit wire serializer


def test_structured_identity_and_profile_isolation(dashboard):
    root, app, _ = dashboard
    first = resolve_dashboard_task_context(request(app, session=session(provider="a:b", org_id="c")), "alpha")
    collided_by_old_delimiters = resolve_dashboard_task_context(request(app, session=session(provider="a", org_id="b:c")), "alpha")
    assert first.principal_id != collided_by_old_delimiters.principal_id
    for overrides in ({"provider": "other"}, {"org_id": "other"}, {"user_id": "other"}):
        changed = resolve_dashboard_task_context(request(app, session=session(**overrides)), "alpha")
        assert changed.principal_id != resolve_dashboard_task_context(request(app, session=session()), "alpha").principal_id
    default = resolve_dashboard_task_context(request(app, session=session(org_id="")))
    beta = resolve_dashboard_task_context(request(app, session=session(org_id="")), "beta")
    assert default.principal_id == beta.principal_id
    assert default.profile_name == "default" and default.profile_home == root
    assert beta.profile_name == "beta" and beta.profile_home == root / "profiles" / "beta"


def test_legacy_token_is_shared_role_across_rotation(dashboard, monkeypatch):
    _, app, server = dashboard
    context = resolve_dashboard_task_context(with_token(request(app, actor="not-a-person-identity"), server), "alpha")
    assert context.principal_id == "shared-dashboard-operator"
    assert context.principal_kind == "shared_dashboard_operator"
    monkeypatch.setattr(server, "_SESSION_TOKEN", "rotated-dashboard-token")
    rotated = resolve_dashboard_task_context(with_token(request(app), server, "rotated-dashboard-token", bearer=True), "alpha")
    assert rotated == context
    with pytest.raises(HTTPException) as denied:
        resolve_dashboard_task_context(with_token(request(app), server), "alpha")
    assert denied.value.status_code == 403


@pytest.mark.parametrize("state", [
    {}, {"auth": SimpleNamespace(source="api_key"), "actor": "claimed-operator"},
    {"session": {"provider": "oauth", "org_id": "org-one", "user_id": "user-one"}},
    {"session": SimpleNamespace(provider="oauth", org_id="org-one", user_id="user-one")},
    {"token_authenticated": True}, {"token_principal": TokenPrincipal("machine", "service")},
    {"session": session(), "token_authenticated": True},
])
def test_unverified_forged_and_machine_contexts_deny(dashboard, state):
    _, app, _ = dashboard
    with pytest.raises(HTTPException) as denied:
        resolve_dashboard_task_context(request(app, **state), "alpha")
    assert denied.value.status_code == 403


@pytest.mark.parametrize("overrides", [{"provider": ""}, {"provider": " "}, {"provider": None},
                                        {"org_id": None}, {"user_id": ""}, {"user_id": 1}, {"user_id": "\ud800"}])
def test_malformed_session_never_falls_back_to_valid_shared_token(dashboard, overrides):
    _, app, server = dashboard
    with pytest.raises(HTTPException) as denied:
        resolve_dashboard_task_context(with_token(request(app, session=session(**overrides)), server), "alpha")
    assert denied.value.status_code == 403


def test_oauth_mode_does_not_accept_legacy_token_without_verified_session(dashboard):
    _, app, server = dashboard
    app.state.auth_required = True
    with pytest.raises(HTTPException) as denied:
        resolve_dashboard_task_context(with_token(request(app), server), "alpha")
    assert denied.value.status_code == 403


@pytest.mark.parametrize("profile,status", [("../outside", 400), ("missing", 404), (123, 400)])
def test_profile_validation_uses_existing_owner(dashboard, profile, status):
    _, app, server = dashboard
    with pytest.raises(HTTPException) as error:
        resolve_dashboard_task_context(with_token(request(app), server), profile)
    assert error.value.status_code == status
