"""Unit tests for auth helpers."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth


def test_create_user_and_verify_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    user = auth.create_user("alice", "hunter2", is_admin=True)

    assert user["username"] == "alice"
    assert auth.verify_user_credentials("alice", "hunter2")["id"] == user["id"]
    assert auth.verify_user_credentials("alice", "wrong-password") is None


def test_session_token_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("bob", "correct-horse", replace=True)

    token = auth.create_session_token(user)
    payload = auth.read_session_token(token)

    assert payload["user_id"] == user["id"]
    assert payload["username"] == "bob"
