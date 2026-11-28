"""A bot asks the record who it answers, and keeps answering through a blip.

These drive `ClaimResolver` over real HTTP against a stand-in gateway rather
than a stubbed session object: the resolver's whole job is the round trip,
the cache in front of it and what it does when the round trip fails, and a
stub that returned a dict would leave all three unexercised.
"""
from __future__ import annotations

from aiohttp import web
from aiohttp.test_utils import TestServer

from hive_surfaces.surface_claim import ClaimResolver


class _Clock:
    """A clock the test moves, so the cache window is driven not waited on."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


async def _gateway(state: dict) -> TestServer:
    """A stand-in for the gateway's claim routes, counting what it served."""

    async def get_claim(request: web.Request) -> web.Response:
        state["reads"] = state.get("reads", 0) + 1
        if state.get("read_fails"):
            return web.Response(status=state.get("fail_status", 503), text="down")
        return web.json_response({"claimed_ref": state.get("claimed_ref", "")})

    async def attempt(request: web.Request) -> web.Response:
        body = await request.json()
        state["attempts"] = state.get("attempts", []) + [body]
        if body.get("text") == state.get("phrase") and not state.get("claimed_ref"):
            state["claimed_ref"] = str(body["sender_ref"])
            return web.json_response(
                {"claimed": True, "reason": "claimed", "claimed_ref": state["claimed_ref"]}
            )
        return web.json_response({"claimed": False, "reason": "mismatch", "claimed_ref": ""})

    app = web.Application()
    app.router.add_get("/broker/minds/{mind_id}/surface-claim", get_claim)
    app.router.add_post("/broker/minds/{mind_id}/surface-claim/attempt", attempt)
    server = TestServer(app)
    await server.start_server()
    return server


def _resolver(server: TestServer, clock) -> ClaimResolver:
    return ClaimResolver(
        str(server.make_url("")), "service-token", "mind-uuid", "telegram",
        ttl_seconds=15.0, clock=clock,
    )


async def test_only_the_claimed_id_is_allowed():
    state = {"claimed_ref": "8776938611"}
    server = await _gateway(state)
    try:
        resolver = _resolver(server, _Clock())
        assert await resolver.is_allowed(8776938611) is True
        assert await resolver.is_allowed(1234) is False
    finally:
        await server.close()


async def test_an_unclaimed_bot_allows_nobody():
    state = {"claimed_ref": ""}
    server = await _gateway(state)
    try:
        resolver = _resolver(server, _Clock())
        assert await resolver.is_allowed(8776938611) is False
        assert await resolver.is_claimed() is False
    finally:
        await server.close()


async def test_the_claim_is_reread_once_the_cache_window_passes():
    state = {"claimed_ref": ""}
    server = await _gateway(state)
    clock = _Clock()
    try:
        resolver = _resolver(server, clock)
        assert await resolver.is_allowed(777) is False
        state["claimed_ref"] = "777"
        # Inside the window the bot still answers from what it was told.
        clock.now += 14.0
        assert await resolver.is_allowed(777) is False
        clock.now += 2.0
        assert await resolver.is_allowed(777) is True
    finally:
        await server.close()


async def test_one_read_serves_every_message_inside_the_window():
    state = {"claimed_ref": "777"}
    server = await _gateway(state)
    clock = _Clock()
    try:
        resolver = _resolver(server, clock)
        for _ in range(5):
            await resolver.is_allowed(777)
        assert state["reads"] == 1
    finally:
        await server.close()


async def test_a_gateway_that_goes_down_keeps_answering_the_same_person():
    state = {"claimed_ref": "777"}
    server = await _gateway(state)
    clock = _Clock()
    try:
        resolver = _resolver(server, clock)
        assert await resolver.is_allowed(777) is True
        state["read_fails"] = True
        clock.now += 60.0
        assert await resolver.is_allowed(777) is True
        assert await resolver.is_allowed(888) is False
    finally:
        await server.close()


async def test_a_gateway_that_was_never_reachable_allows_nobody():
    resolver = ClaimResolver(
        "http://127.0.0.1:1", "service-token", "mind-uuid", "telegram", timeout_s=0.25
    )
    assert await resolver.is_allowed(777) is False


async def test_the_phrase_claims_and_the_next_message_is_answered_without_a_reread():
    state = {"claimed_ref": "", "phrase": "anchor basalt cinder"}
    server = await _gateway(state)
    clock = _Clock()
    try:
        resolver = _resolver(server, clock)
        assert await resolver.is_allowed(777) is False
        answer = await resolver.attempt_claim(777, "anchor basalt cinder")
        assert answer.claimed is True
        reads_before = state["reads"]
        assert await resolver.is_allowed(777) is True
        assert state["reads"] == reads_before
    finally:
        await server.close()


async def test_a_wrong_phrase_claims_nothing_and_the_sender_stays_refused():
    state = {"claimed_ref": "", "phrase": "anchor basalt cinder"}
    server = await _gateway(state)
    try:
        resolver = _resolver(server, _Clock())
        answer = await resolver.attempt_claim(777, "let me in")
        assert answer.claimed is False
        assert answer.reason == "mismatch"
        assert await resolver.is_allowed(777) is False
    finally:
        await server.close()


async def test_the_message_offered_for_judgement_is_what_the_person_typed():
    state = {"claimed_ref": "", "phrase": "anchor basalt cinder"}
    server = await _gateway(state)
    try:
        resolver = _resolver(server, _Clock())
        await resolver.attempt_claim(777, "  Anchor Basalt Cinder. ")
        assert state["attempts"][-1] == {
            "surface": "telegram",
            "sender_ref": "777",
            "text": "  Anchor Basalt Cinder. ",
        }
    finally:
        await server.close()


async def test_a_gateway_with_no_claim_routes_cannot_decide():
    """Two services never deploy in the same instant.

    A mind running this code against a gateway that predates the claim routes
    must not refuse its own owner, so the resolver says it cannot answer and
    the surface falls back to the allow-list it was configured with.
    """
    app = web.Application()  # no claim routes at all
    server = TestServer(app)
    await server.start_server()
    try:
        resolver = _resolver(server, _Clock())
        assert await resolver.is_allowed(777) is False
        assert resolver.supported is False
    finally:
        await server.close()


async def test_a_gateway_that_refuses_the_credential_cannot_decide_either():
    """A mind with no credential of its own gets 401, not 404.

    Both leave the console showing an accepted token over an unclaimed bot
    with nothing anywhere saying the word credential, so refusing the owner
    on that evidence is the outcome worth avoiding.
    """
    async def refuse(request):
        return web.Response(status=401, text="no holder")

    app = web.Application()
    app.router.add_get("/broker/minds/{mind_id}/surface-claim", refuse)
    server = TestServer(app)
    await server.start_server()
    try:
        resolver = _resolver(server, _Clock())
        assert await resolver.is_allowed(777) is False
        assert resolver.supported is False
    finally:
        await server.close()


async def test_a_resolver_that_has_never_been_answered_decides_nothing():
    resolver = ClaimResolver(
        "http://127.0.0.1:1", "service-token", "mind-uuid", "telegram", timeout_s=0.25
    )
    assert resolver.supported is False
    await resolver.is_allowed(777)
    assert resolver.supported is False


async def test_a_gateway_that_comes_back_is_believed_again():
    """The fallback is a spell, not a sentence for the life of the process.

    A transient 404 — a proxy closing /broker, comms restarting — pinned
    every bot in the hive to its config.yaml until somebody restarted it.
    """
    state = {"claimed_ref": "777", "read_fails": True, "fail_status": 404}
    server = await _gateway(state)
    clock = _Clock()
    try:
        resolver = _resolver(server, clock)
        assert await resolver.is_allowed(777) is False
        assert resolver.supported is False
        state["read_fails"] = False
        clock.now += 46.0
        assert await resolver.is_allowed(777) is True
        assert resolver.supported is True
    finally:
        await server.close()


async def test_a_gateway_that_answers_keeps_the_resolver_in_charge():
    state = {"claimed_ref": "777"}
    server = await _gateway(state)
    try:
        resolver = _resolver(server, _Clock())
        assert await resolver.is_allowed(777) is True
        assert resolver.supported is True
    finally:
        await server.close()


async def test_a_blip_after_a_good_answer_keeps_answering_the_same_person():
    """Distinct from never having been answered: there is a claim to stand on."""
    state = {"claimed_ref": "777"}
    server = await _gateway(state)
    clock = _Clock()
    try:
        resolver = _resolver(server, clock)
        assert await resolver.is_allowed(777) is True
        state["read_fails"] = True
        clock.now += 20.0
        assert await resolver.is_allowed(777) is True
        assert await resolver.is_allowed(888) is False
    finally:
        await server.close()
