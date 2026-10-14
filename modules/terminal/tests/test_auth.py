"""Unit tests for the built-in account store's helpers."""

import pytest

import auth


def test_a_created_user_signs_in_with_their_password_and_only_that(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    user = auth.create_user("alice", "hunter2", is_admin=True)
    store = auth.BuiltinStore()

    signed_in, token = store.sign_in("alice", "hunter2")
    assert signed_in["id"] == user["id"]
    assert store.user_for_token(token)["username"] == "alice"
    with pytest.raises(auth.SignInRefused):
        store.sign_in("alice", "wrong-password")


def test_session_token_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("bob", "correct-horse", replace=True)

    token = auth.create_session_token(user)
    payload = auth.read_session_token(token)

    assert payload["user_id"] == user["id"]
    assert payload["username"] == "bob"


def test_an_older_db_moves_is_admin_into_the_admins_group(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "hive.db"
    monkeypatch.setenv("HIVE_DB_PATH", str(path))
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, "
        "password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, "
        "created_at INTEGER NOT NULL, disabled_at INTEGER)"
    )
    old.execute(
        "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, 1, 0)",
        ("daniel", auth.hash_password("daniels-pass")),
    )
    old.execute(
        "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, 0, 0)",
        ("guest", auth.hash_password("guests-pass")),
    )
    old.commit()
    old.close()

    store = auth.BuiltinStore()
    assert store.get_user("daniel")["is_admin"] is True
    assert store.get_user("guest")["is_admin"] is False
    assert store.sign_in("daniel", "daniels-pass")[0]["must_change"] is False

    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(users)")}
    assert "is_admin" not in columns
