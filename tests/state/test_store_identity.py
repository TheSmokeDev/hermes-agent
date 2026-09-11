"""Comparable physical-store identity and honest normal replacement/recovery behavior."""
import shutil

import pytest

from hermes_state import SessionDB
from hermes_state_store_identity import StoreIdentityUnavailable, get_store_id


def test_independent_handles_and_normal_writes_share_identity(tmp_path):
    path = tmp_path / "state.db"
    first, second = SessionDB(path), SessionDB(path)
    try:
        identity = get_store_id(first)
        assert identity == get_store_id(second)
        first.create_session("same-session", source="test")
        first.append_message("same-session", "user", "ordinary write")
        assert get_store_id(first) == get_store_id(second) == identity
    finally:
        first.close()
        second.close()
    reopened = SessionDB(path)
    try:
        assert get_store_id(reopened) == identity
    finally:
        reopened.close()


def test_copied_and_recovered_stores_do_not_reuse_physical_identity(tmp_path):
    from hermes_cli.session_recovery import recover_session_database
    source = tmp_path / "source.db"
    db = SessionDB(source)
    db.create_session("same-session", source="test")
    original = get_store_id(db)
    db.close()
    clone = tmp_path / "clone.db"
    shutil.copy2(source, clone)
    copied = SessionDB(clone)
    try:
        assert copied.get_session("same-session") is not None
        assert get_store_id(copied) != original
    finally:
        copied.close()
    recovered_path = tmp_path / "recovered.db"
    recover_session_database(source, recovered_path, work_dir=tmp_path)
    recovered = SessionDB(recovered_path)
    try:
        assert recovered.get_session("same-session") is not None
        assert get_store_id(recovered) != original
    finally:
        recovered.close()
    # Recovery preserves the generation metadata; an in-place install also preserves the inode.
    # The canonical application-ID stamp must still distinguish the recovered generation.
    shutil.copyfile(recovered_path, source)
    installed = SessionDB(source)
    try:
        assert get_store_id(installed) != original
    finally:
        installed.close()


def test_unknown_file_identity_refuses_instead_of_guessing(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    try:
        monkeypatch.setattr("hermes_state_store_identity.stat_db_file_identity", lambda _: None)
        with pytest.raises(StoreIdentityUnavailable):
            get_store_id(db)
    finally:
        db.close()
