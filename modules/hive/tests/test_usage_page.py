"""What each person's minds cost (story 2.1.17).

DECIDED 2026-11-15 by Daniel: one proxy key per mind, and usage totalled per
person across the minds they own. Driven through the console's own HTTP
surface; the inference proxy's meter is the stand-in hive's, so what is
exercised is the route table, the read of the meter and the grouping by owner.
"""

import pytest


@pytest.fixture(autouse=True)
def _proxy_is_the_stand_in(fake_comms, monkeypatch):
    monkeypatch.setenv("INFERENCE_PROXY_URL", fake_comms.url)
    monkeypatch.setenv("PROXY_CREDENTIAL_ADMIN_KEY", "console-proxy-admin")


def test_a_second_users_mind_is_counted_under_them_and_only_theirs_is_shown(console):
    admin = console.admin()
    sloan = console.settled_user(admin, "sloan", "sloan-real-password-1")

    body = sloan.get("/api/usage").json()

    (person,) = body["people"]
    assert person["username"] == "sloan"
    assert [m["holder"] for m in person["minds"]] == ["arnold"]
    assert (person["requests"], person["total_tokens"]) == (3, 700)
    assert body["services"] == []


def test_an_admin_sees_what_each_person_costs_and_the_hives_own_services(console):
    body = console.admin().get("/api/usage").json()

    people = {p["username"]: p for p in body["people"]}
    assert people["daniel"]["cost"] == 1.5
    assert people["sloan"]["cost"] == 0.25
    assert [s["holder"] for s in body["services"]] == ["hive-tools"]


def test_a_meter_that_cannot_be_read_is_said_not_shown_as_nothing(console, monkeypatch):
    monkeypatch.setenv("INFERENCE_PROXY_URL", "http://127.0.0.1:9")

    assert console.admin().get("/api/usage").status_code == 503
