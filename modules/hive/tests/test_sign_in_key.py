"""The hive's own sign-in makes the key (feature 834, story 912).

B20 signing out ends the key at once, B21 a sign-in stops twelve hours after
it was made however much it was used, B24 a hive with only its own sign-in,
and no authentication service, signs a person in with a key the gateway
accepts. The sign-in is driven through the console's real `/login` and
`/logout`; "the gateway accepts it" is the gateway's own per-request check
(`comms.user_access.credential_for_token`) over the same record. The clock is
the record's (`core.now`), moved by the test: nothing sleeps.
"""

import accounts
import httpx
import pytest
from multiuser_support import SEED_PASSWORD, SEED_USERNAME
from comms import user_access

import auth
from hive_data import core

HOUR = 3600.0
START = 1_800_000_000.0


@pytest.fixture
def clock(monkeypatch):
    moment = {"t": START}
    monkeypatch.setattr(core, "now", lambda: moment["t"])
    return moment


def _key_of(client) -> str:
    return client.cookies.get(auth.SESSION_COOKIE_NAME)


def _gateway_knows(key: str):
    return accounts.run(user_access.credential_for_token(key))


def test_signing_out_ends_the_key_at_once_and_the_gateway_refuses_the_same_value(
        console, clock):
    client, signed_in = console.sign_in(SEED_USERNAME, SEED_PASSWORD)
    assert signed_in.status_code == 303
    key = _key_of(client)
    other_device, _ = console.sign_in(SEED_USERNAME, SEED_PASSWORD)
    assert _gateway_knows(key) is not None

    client.post("/logout", follow_redirects=False)

    assert _gateway_knows(key) is None
    client.cookies.set(auth.SESSION_COOKIE_NAME, key)
    assert client.get("/overview", follow_redirects=False).status_code == 303
    # Only that device: the other stays signed in.
    assert _gateway_knows(_key_of(other_device)) is not None


def test_a_sign_in_stops_twelve_hours_after_it_was_made_however_much_it_was_used(
        console, clock):
    client, _ = console.sign_in(SEED_USERNAME, SEED_PASSWORD)
    key = _key_of(client)

    for hours in (1, 5, 9, 11):  # used steadily; use must not lengthen it
        clock["t"] = START + hours * HOUR
        assert client.get("/overview", follow_redirects=False).status_code == 200
    clock["t"] = START + 12 * HOUR - 1
    assert _gateway_knows(key) is not None
    assert client.get("/overview", follow_redirects=False).status_code == 200

    clock["t"] = START + 12 * HOUR
    assert _gateway_knows(key) is None
    refused = client.get("/overview", follow_redirects=False)
    assert refused.status_code == 303
    assert refused.headers["location"].startswith("/login")


def test_a_hive_with_only_its_own_sign_in_makes_a_key_the_gateway_accepts_and_calls_nothing_else(
        console, clock, monkeypatch):
    for name in ("AUTH_SERVICE_URL", "AUTH_SERVICE_SITE", "HIVE_AUTH_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    asked: list[str] = []

    async def no_outside_calls(self, method, url, **kwargs):
        asked.append(f"{method} {url}")
        raise AssertionError("the sign-in reached outside the hive")

    monkeypatch.setattr(httpx.AsyncClient, "request", no_outside_calls)

    client, signed_in = console.sign_in(SEED_USERNAME, SEED_PASSWORD)

    assert signed_in.status_code == 303
    known = _gateway_knows(_key_of(client))
    assert known is not None and known.username == SEED_USERNAME
    assert client.get("/overview", follow_redirects=False).status_code == 200
    assert asked == []
    assert not hasattr(auth, "AuthServiceStore")


def test_an_install_whose_settings_still_name_the_old_service_is_told_what_to_change(
        monkeypatch):
    monkeypatch.setenv("AUTH_SERVICE_URL", "https://auth.example.invalid")

    with pytest.raises(auth.OldSettings) as told:
        auth.refuse_old_settings()

    assert "AUTH_SERVICE_URL" in str(told.value)
    assert "signs people in itself" in str(told.value)
