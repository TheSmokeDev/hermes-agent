"""Opaque same-store proof using SessionDB's existing generation and physical file identity."""
from __future__ import annotations

import hashlib
import json
import sqlite3

from hermes_state_common import stat_db_file_identity
from hermes_state_errors import _STATE_DB_GENERATION_KEY


class StoreIdentityUnavailable(RuntimeError):
    """The actual open store cannot supply a trustworthy comparable identity."""


def get_store_id(db) -> str:
    """Read-only proof, never permission. Copies/recovery files differ despite copied metadata.

    Unknown physical identity or absent legacy generation fails closed. In-place restoration that
    preserves generation, application stamp and physical identity is outside this lifecycle detector.
    """
    try:
        db._raise_if_db_replaced()
        identity = stat_db_file_identity(db.db_path)
        if identity is None or identity != db._db_file_identity:
            raise StoreIdentityUnavailable("Canonical store identity unavailable")
        with db._read_ctx() as conn:
            row = conn.execute("SELECT value FROM state_meta WHERE key=?", (_STATE_DB_GENERATION_KEY,)).fetchone()
        if row is None or not isinstance(row[0], str) or not row[0]:
            raise StoreIdentityUnavailable("Canonical store generation unavailable")
        application_id = db._db_file_application_id
        if type(application_id) is not int or application_id <= 0:
            raise StoreIdentityUnavailable("Canonical store application stamp unavailable")
        db._raise_if_db_replaced()
        if stat_db_file_identity(db.db_path) != identity:
            raise StoreIdentityUnavailable("Canonical store changed during identity read")
        encoded = json.dumps(
            ["hermes-store-v1", row[0], application_id, list(identity)], separators=(",", ":")
        ).encode()
    except (OSError, sqlite3.Error, RuntimeError, AttributeError, UnicodeError):
        raise StoreIdentityUnavailable("Canonical profile store identity unavailable") from None
    return "hermes-store-v1-" + hashlib.sha256(encoded).hexdigest()
