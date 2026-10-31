"""Unit tests for the built-in account store's helpers."""

import sqlite3

import auth
import store as data_store


async def test_session_token_round_trip(monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = await auth.create_user("bob", "correct-horse", replace=True)

    token = await auth.create_session_token(user)
    payload = await auth.read_session_token(token)

    assert payload["user_id"] == user["id"]
    assert payload["username"] == "bob"


async def test_an_older_db_moves_is_admin_into_the_admins_group(tmp_path):
    """A file that held admin as a column reads with admin as a group.

    The move happens as the store opens, in one transaction, so no reader
    ever sees an admin who is neither — and the column is gone afterwards,
    which is what stops the two answers existing side by side.
    """
    path = tmp_path / "legacy.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL "
        "UNIQUE, password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL "
        "DEFAULT 0, created_at INTEGER NOT NULL, disabled_at INTEGER)"
    )
    old.execute(
        "INSERT INTO users (username, password_hash, is_admin, created_at) "
        "VALUES (?, ?, 1, 0)",
        ("daniel", auth.hash_password("daniels-pass")),
    )
    old.execute(
        "INSERT INTO users (username, password_hash, is_admin, created_at) "
        "VALUES (?, ?, 0, 0)",
        ("guest", auth.hash_password("guests-pass")),
    )
    old.commit()
    old.close()

    await data_store.close()
    await data_store.open(path)

    store = auth.BuiltinStore()
    assert (await store.get_user("daniel"))["is_admin"] is True
    assert (await store.get_user("guest"))["is_admin"] is False
    assert (await store.sign_in("daniel", "daniels-pass"))[0][
        "must_change"] is False

    columns = {row[1] for row in
               sqlite3.connect(path).execute("PRAGMA table_info(users)")}
    assert "is_admin" not in columns
