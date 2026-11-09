"""Unit tests for the built-in account store's helpers."""

import auth


async def test_session_token_round_trip(monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = await auth.create_user("bob", "correct-horse", replace=True)

    token = await auth.create_session_token(user)
    payload = await auth.read_session_token(token)

    assert payload["user_id"] == user["id"]
    assert payload["username"] == "bob"


