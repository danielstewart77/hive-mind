"""Registering a bot, and deciding the one person it answers.

Every step goes through the console's own HTTP surface against the comms
stand-in, so what is exercised is the route table in front of the routes, the
relay to each mind, and the rows the page is built from.
"""

import pytest
from multiuser_support import MINDS


@pytest.fixture(autouse=True)
def _empty_hive(fake_comms):
    fake_comms.claims.clear()
    fake_comms.tokens.clear()
    fake_comms.unreachable.clear()
    yield
    fake_comms.claims.clear()
    fake_comms.tokens.clear()
    fake_comms.unreachable.clear()


def _owned_minds(rows):
    return sorted({row["mind"] for row in rows})


def test_the_listing_holds_only_the_minds_the_signed_in_user_owns(console):
    admin = console.admin()

    rows = admin.get("/api/bots").json()["bots"]

    # Every mind, because the seeded account is an admin — and both surfaces
    # of each, since one mind can hold a Telegram bot and a Discord bot.
    assert _owned_minds(rows) == sorted({m["name"] for m in MINDS})
    assert sorted({row["surface"] for row in rows}) == ["discord", "telegram"]


def test_a_person_sees_their_own_minds_bots_and_not_another_persons(console):
    admin = console.admin()
    sloan = console.settled_user(admin, "sloan", "sloan-real-password-1")

    rows = sloan.get("/api/bots").json()["bots"]

    assert _owned_minds(rows) == ["Arnold"]


def test_a_person_cannot_register_a_bot_against_a_mind_they_do_not_own(console):
    admin = console.admin()
    sloan = console.settled_user(admin, "sloan", "sloan-real-password-1")

    refused = sloan.put(
        "/api/minds/skippy/surface-token?surface=telegram",
        json={"token": "good-token"},
    )

    assert refused.status_code in (403, 404)


def test_a_token_the_provider_refuses_is_reported_and_stores_nothing(console):
    admin = console.admin()

    response = admin.put(
        "/api/minds/skippy/surface-token?surface=telegram",
        json={"token": "mistyped"},
    )

    assert response.status_code == 400
    assert "refused" in response.text


def test_an_accepted_token_names_the_bot_it_authenticates_as(console):
    admin = console.admin()

    response = admin.put(
        "/api/minds/skippy/surface-token?surface=discord",
        json={"token": "good-token"},
    )

    assert response.status_code == 200
    # The surface asked for is the surface answered about — one mind holds
    # both bots, so a row that asked Telegram's question would report the
    # wrong bot under a Discord label.
    assert response.json()["bot_username"] == "discord-bot"


def test_a_mind_that_is_down_fails_its_own_row_and_not_the_page(console, fake_comms):
    admin = console.admin()
    fake_comms.unreachable.add(("", "telegram"))

    rows = admin.get("/api/bots").json()["bots"]

    assert rows, "the page still answers"
    telegram = [row for row in rows if row["surface"] == "telegram"]
    discord = [row for row in rows if row["surface"] == "discord"]
    assert all(row["reachable"] is False for row in telegram)
    assert all(row["reachable"] is True for row in discord)


def test_the_phrase_is_minted_for_the_mind_and_surface_named(console, fake_comms):
    admin = console.admin()

    body = admin.post("/api/minds/zack/bot-claim/phrase?surface=discord").json()

    assert body["phrase"]
    assert body["surface"] == "discord"
    assert ("m-zack", "discord") in fake_comms.claims


def test_an_already_claimed_bot_refuses_a_fresh_phrase(console, fake_comms):
    admin = console.admin()
    fake_comms.claims[("m-skippy", "telegram")] = {"claimed_ref": "8776938611"}

    response = admin.post("/api/minds/skippy/bot-claim/phrase?surface=telegram")

    assert response.status_code == 409


def test_the_listing_names_the_person_a_bot_answers(console, fake_comms):
    admin = console.admin()
    fake_comms.claims[("m-skippy", "telegram")] = {"claimed_ref": "8776938611"}

    rows = admin.get("/api/bots").json()["bots"]
    row = next(r for r in rows if r["mind"] == "skippy" and r["surface"] == "telegram")

    assert row["claim"]["claimed"] is True
    assert row["claim"]["claimed_ref"] == "8776938611"


def test_releasing_a_claim_leaves_the_bot_claimable_again(console, fake_comms):
    admin = console.admin()
    fake_comms.claims[("m-skippy", "telegram")] = {"claimed_ref": "8776938611"}

    cleared = admin.delete("/api/minds/skippy/bot-claim?surface=telegram")
    state = admin.get("/api/minds/skippy/bot-claim?surface=telegram").json()

    assert cleared.status_code == 200
    assert state["claimed"] is False
    assert admin.post(
        "/api/minds/skippy/bot-claim/phrase?surface=telegram"
    ).status_code == 200


def test_a_gateway_with_no_claim_routes_reads_as_unknown_not_unclaimed(
    console, fake_comms
):
    """The remedy for one is to go and look at the gateway, and for the other
    to have somebody send a phrase."""
    admin = console.admin()
    fake_comms.absent["claim_routes"] = True
    try:
        state = admin.get("/api/minds/skippy/bot-claim?surface=telegram")
    finally:
        fake_comms.absent["claim_routes"] = False

    assert state.status_code == 200
    assert state.json()["known"] is False
