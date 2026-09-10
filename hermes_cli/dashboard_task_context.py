"""Server-only dashboard task identity/profile context for host plugins.

Authentication belongs to dashboard middleware. This helper consumes its verified session or
the existing local dashboard-token verifier; it does not grant task or approval permissions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request

from hermes_cli.dashboard_auth.base import Session
from hermes_cli.web_server_cron import _cron_profile_home
from hermes_cli.web_server_sessions import _open_session_db_for_profile
from hermes_state_registry import release_or_close
from hermes_state_store_identity import get_store_id


@dataclass(frozen=True, slots=True)
class DashboardTaskContext:
    """Keep on the server; consumers scope principal identity by configured host and profile."""

    principal_id: str
    principal_kind: Literal["verified_session", "shared_dashboard_operator"]
    profile_name: str
    profile_home: Path = field(repr=False)
    store_id: str


def _denied():
    raise HTTPException(status_code=403, detail="Verified dashboard operator context required")


def _dashboard_principal(request: Request):
    state = request.state
    if getattr(state, "token_authenticated", False) or getattr(state, "token_principal", None) is not None:
        _denied()  # Registered machine-token routes have a different, unsupported authority contract.
    session = getattr(state, "session", None)
    if session is not None:
        if not isinstance(session, Session):
            _denied()
        identity = (session.provider, session.org_id, session.user_id)
        if (not all(isinstance(value, str) for value in identity)
                or not session.provider.strip() or not session.user_id.strip()):
            _denied()
        try:
            encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except UnicodeError:
            _denied()
        return "dashboard-session-" + hashlib.sha256(encoded).hexdigest(), "verified_session"
    app = request.scope.get("app")
    if app is None or getattr(app.state, "auth_required", False):
        _denied()
    from hermes_cli.web_server import _has_valid_session_token
    if not _has_valid_session_token(request):
        _denied()
    # Token holders share one role. Rotation changes credentials, not the role's identity.
    return "shared-dashboard-operator", "shared_dashboard_operator"


def resolve_dashboard_task_context(request: Request, profile: str | None = None) -> DashboardTaskContext:
    """Resolve verified operator identity and canonical profile without parsing credentials.

    ``request.state.session`` must be middleware-owned, not deserialized client claims. Session
    freshness remains the existing middleware's responsibility. Never return this object (especially
    profile_home) to browser code; a consumer should issue its own opaque connection reference.
    """
    principal_id, principal_kind = _dashboard_principal(request)
    if profile is not None and not isinstance(profile, str):
        raise HTTPException(status_code=400, detail="profile must be a string")
    profile_name, profile_home = _cron_profile_home(profile)
    try:
        db = _open_session_db_for_profile(profile, read_only=True)
        try:
            store_id = get_store_id(db)
        finally:
            release_or_close(db)
    except (OSError, sqlite3.Error, RuntimeError):
        raise HTTPException(status_code=503, detail="Canonical profile store identity unavailable") from None
    return DashboardTaskContext(principal_id, principal_kind, profile_name, Path(profile_home), store_id)
