"""How this stack's surface containers learn who their bot answers.

Every one of these drives the real `ClaimResolver` over real HTTP against a
stand-in gateway. The resolver's whole job here is the round trip — which
mind it asks about, what it offers on adoption, and what it does when the
gateway is not up yet — and a stubbed session returning a dict would leave
all three unexercised.

The stand-in is the transport and nothing else: the code deciding which
reference is offered, and for which surface, is imported and called.
"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from hive_surfaces import surface_claim

from surfaces import claims


@pytest.fixture(autouse=True)
def _no_resolver_left_installed():
    surface_claim.forget_all()
    yield
    surface_claim.forget_all()


async def _gateway(state: dict) -> TestServer:
    """The gateway's claim routes, recording what was asked of them."""

    async def get_claim(request: web.Request) -> web.Response:
        state.setdefault("reads", []).append(
            (request.match_info["mind_id"], request.query.get("surface", ""))
        )
        return web.json_response({"claimed_ref": state.get("claimed_ref", "")})

    async def adopt(request: web.Request) -> web.Response:
        body = await request.json()
        state.setdefault("adopts", []).append(
            (request.match_info["mind_id"], body.get("surface"), body.get("sender_ref"))
        )
        refusals = state.get("refuse_adopts", 0)
        if refusals:
            state["refuse_adopts"] = refusals - 1
            return web.Response(status=503, text="not up yet")
        if state.get("claimed_ref"):
            return web.json_response(
                {"claimed": False, "reason": "already_decided",
                 "claimed_ref": state["claimed_ref"]}
            )
        state["claimed_ref"] = str(body["sender_ref"])
        return web.json_response(
            {"claimed": True, "reason": "adopted", "claimed_ref": state["claimed_ref"]}
        )

    app = web.Application()
    app.router.add_get("/broker/minds/{mind_id}/surface-claim", get_claim)
    app.router.add_post("/broker/minds/{mind_id}/surface-claim/adopt", adopt)
    server = TestServer(app)
    await server.start_server()
    return server


def _env(monkeypatch, server: TestServer, mind_id: str) -> None:
    monkeypatch.setenv("MIND_ID", mind_id)
    monkeypatch.setenv("COMMS_URL", str(server.make_url("")))
    monkeypatch.setenv("COMMS_BEARER_TOKEN", "service-token")


class TestWhichMindAContainerAsksAbout:
    async def test_the_resolver_asks_about_this_container_s_own_mind(
        self, monkeypatch
    ) -> None:
        """Five Telegram containers run one image off one config file. The
        mind id is the only thing separating them, so a resolver built from
        anything shared would answer one mind's question with another's."""
        state: dict = {"claimed_ref": "8776938611"}
        server = await _gateway(state)
        try:
            _env(monkeypatch, server, "bob-uuid")
            claims.install_claim_resolver("telegram")
            resolver = surface_claim.installed("telegram")

            await resolver.claimed_ref()
        finally:
            await server.close()

        assert state["reads"] == [("bob-uuid", "telegram")]

    async def test_a_container_installs_only_the_surface_it_runs(
        self, monkeypatch
    ) -> None:
        """A Telegram container has no Discord bot. Installing one anyway
        writes a claim for a bot that does not exist, which the bots page
        then reports as claimed."""
        state: dict = {}
        server = await _gateway(state)
        try:
            _env(monkeypatch, server, "bob-uuid")
            claims.install_claim_resolver("telegram")
        finally:
            await server.close()

        assert surface_claim.installed("discord") is None


class TestWhatAdoptionOffers:
    async def test_adoption_offers_the_named_reference_for_this_mind(
        self, monkeypatch
    ) -> None:
        state: dict = {}
        server = await _gateway(state)
        try:
            _env(monkeypatch, server, "nagatha-uuid")
            monkeypatch.setenv("SURFACE_CLAIM_ADOPT_REF", "8776938611")
            claims.install_claim_resolver("telegram")

            outcome = await claims.adopt_claim("telegram")
        finally:
            await server.close()

        assert state["adopts"] == [("nagatha-uuid", "telegram", "8776938611")]
        assert outcome == "adopted"

    async def test_a_list_naming_two_people_adopts_nobody(self, monkeypatch) -> None:
        """Guessing which of several owns the bot is how somebody else's bot
        changes hands in silence."""
        state: dict = {}
        server = await _gateway(state)
        try:
            _env(monkeypatch, server, "ada-uuid")
            monkeypatch.delenv("SURFACE_CLAIM_ADOPT_REF", raising=False)
            monkeypatch.setattr(
                claims, "_configured_list",
                lambda surface: [8776938611, 8636028257],
            )

            outcome = await claims.adopt_claim("telegram")
        finally:
            await server.close()

        assert state.get("adopts") is None
        assert outcome == "no_reference"

    async def test_a_list_naming_one_person_carries_that_person_across(
        self, monkeypatch
    ) -> None:
        state: dict = {}
        server = await _gateway(state)
        try:
            _env(monkeypatch, server, "ada-uuid")
            monkeypatch.delenv("SURFACE_CLAIM_ADOPT_REF", raising=False)
            monkeypatch.setattr(
                claims, "_configured_list", lambda surface: [839112201109110825]
            )
            claims.install_claim_resolver("discord")

            outcome = await claims.adopt_claim("discord")
        finally:
            await server.close()

        assert state["adopts"] == [
            ("ada-uuid", "discord", "839112201109110825")
        ]
        assert outcome == "adopted"


class TestAGatewayThatIsNotUpYet:
    async def test_adoption_retries_until_the_gateway_answers(
        self, monkeypatch
    ) -> None:
        """Nothing orders a bot container after hive-comms. One un-retried
        call at boot means a refused connection leaves the bot answering
        nobody for the life of the process, with no line saying so."""
        state: dict = {"refuse_adopts": 2}
        slept: list[float] = []

        async def _sleep(seconds: float) -> None:
            slept.append(seconds)

        server = await _gateway(state)
        try:
            _env(monkeypatch, server, "bilby-uuid")
            monkeypatch.setenv("SURFACE_CLAIM_ADOPT_REF", "8776938611")
            claims.install_claim_resolver("telegram")

            outcome = await claims.adopt_claim("telegram", sleep=_sleep)
        finally:
            await server.close()

        assert outcome == "adopted"
        assert len(state["adopts"]) == 3
        assert len(slept) == 2

    async def test_adoption_waits_out_a_gateway_that_is_merely_late(
        self, monkeypatch
    ) -> None:
        """A budget was worse than no retry at all. Running out does not leave
        the bot on its allow-list: the first message after comms comes up
        reads an empty claim, clears the backoff, and the bot refuses its own
        owner for the life of a process `restart: unless-stopped` will never
        replace."""
        state: dict = {"refuse_adopts": 20}
        slept: list[float] = []

        async def _sleep(seconds: float) -> None:
            slept.append(seconds)

        server = await _gateway(state)
        try:
            _env(monkeypatch, server, "ada-uuid")
            monkeypatch.setenv("SURFACE_CLAIM_ADOPT_REF", "8776938611")
            claims.install_claim_resolver("telegram")

            outcome = await claims.adopt_claim("telegram", sleep=_sleep)
        finally:
            await server.close()

        assert outcome == "adopted"
        assert len(state["adopts"]) == 21
        assert len(slept) == 20

    async def test_a_gateway_with_no_claim_routes_is_not_waited_on(
        self, monkeypatch
    ) -> None:
        """A 404 is a hive that has not deployed the routes. They are not
        going to appear while this bot holds its first poll, and falling back
        to the configured list is what a half-deployed hive needs."""
        asks: list[str] = []

        async def absent(request: web.Request) -> web.Response:
            asks.append(request.match_info["mind_id"])
            return web.Response(status=404, text="no such route")

        app = web.Application()
        app.router.add_post(
            "/broker/minds/{mind_id}/surface-claim/adopt", absent
        )
        server = TestServer(app)
        await server.start_server()

        async def _sleep(_seconds: float) -> None:
            return None

        try:
            _env(monkeypatch, server, "bob-uuid")
            monkeypatch.setenv("SURFACE_CLAIM_ADOPT_REF", "8776938611")
            claims.install_claim_resolver("telegram")

            outcome = await claims.adopt_claim("telegram", sleep=_sleep)
        finally:
            await server.close()

        assert outcome == "unsupported"
        assert len(asks) == claims.UNSUPPORTED_ATTEMPTS

    async def test_adoption_gives_up_and_says_which_way_it_failed(
        self, monkeypatch
    ) -> None:
        state: dict = {"refuse_adopts": 99}
        server = await _gateway(state)
        try:
            _env(monkeypatch, server, "cypher-uuid")
            monkeypatch.setenv("SURFACE_CLAIM_ADOPT_REF", "8776938611")
            claims.install_claim_resolver("telegram")

            async def _sleep(_seconds: float) -> None:
                return None

            outcome = await claims.adopt_claim(
                "telegram", attempts=3, sleep=_sleep
            )
        finally:
            await server.close()

        assert outcome == "unreachable"
        assert len(state["adopts"]) == 3



class TestWhatTheFallbackListHolds:
    def test_the_fallback_names_only_the_adopted_person(self, monkeypatch) -> None:
        """The surfaces fall back to the configured list for 45 seconds after
        every failed read. A shared two-name list there re-authorises the
        second person on every mind at once, every time the gateway blips."""
        monkeypatch.setenv("SURFACE_CLAIM_ADOPT_REF", "8776938611")
        monkeypatch.setattr(
            claims, "_configured_list", lambda surface: [8776938611, 8636028257]
        )

        assert claims.fallback_list("telegram") == [8776938611]

    def test_an_unresolved_reference_leaves_the_configured_list_alone(
        self, monkeypatch
    ) -> None:
        """With nothing to narrow it to, narrowing it to nothing would take
        the bot dark on the first blip rather than on a decision."""
        monkeypatch.delenv("SURFACE_CLAIM_ADOPT_REF", raising=False)
        monkeypatch.setattr(
            claims, "_configured_list", lambda surface: [8776938611, 8636028257]
        )

        assert claims.fallback_list("telegram") == [8776938611, 8636028257]


class TestWhatAContainerDoesBeforeItPolls:
    async def test_the_claim_is_adopted_before_the_telegram_bot_runs(
        self, monkeypatch
    ) -> None:
        """Order is the behaviour. A surface that starts polling first answers
        its first messages from the configured list, and the claim this whole
        mechanism exists for decides nothing for them."""
        import surfaces.telegram as entry

        state: dict = {}
        server = await _gateway(state)
        seen: dict = {}

        async def _run_bot() -> None:
            resolver = surface_claim.installed("telegram")
            seen["resolver_installed"] = resolver is not None
            seen["adopts_by_now"] = len(state.get("adopts", []))

        try:
            _env(monkeypatch, server, "ada-uuid")
            monkeypatch.setenv("SURFACE_CLAIM_ADOPT_REF", "8776938611")
            monkeypatch.setattr(entry, "run_telegram_bot", _run_bot)

            await entry._run()
        finally:
            await server.close()

        assert seen["resolver_installed"] is True
        assert seen["adopts_by_now"] == 1
        assert state["adopts"] == [("ada-uuid", "telegram", "8776938611")]

    async def test_the_claim_is_adopted_before_the_discord_bot_runs(
        self, monkeypatch
    ) -> None:
        import surfaces.discord as entry

        state: dict = {}
        server = await _gateway(state)
        seen: dict = {}

        async def _run_bot() -> None:
            seen["resolver_installed"] = (
                surface_claim.installed("discord") is not None
            )
            seen["adopts_by_now"] = len(state.get("adopts", []))

        try:
            _env(monkeypatch, server, "ada-uuid")
            monkeypatch.setenv("SURFACE_CLAIM_ADOPT_REF", "839112201109110825")
            monkeypatch.setattr(entry, "run_discord_bot", _run_bot)

            await entry._run()
        finally:
            await server.close()

        assert seen["resolver_installed"] is True
        assert seen["adopts_by_now"] == 1

    async def test_the_surface_is_handed_the_narrowed_list_not_the_shared_one(
        self, monkeypatch
    ) -> None:
        """`fallback_list` is guarded on its own, but what the bot authorises
        by is the `SurfaceConfig` it was configured with — one layer further
        on, and the layer the requirement is about."""
        import surfaces.telegram as entry
        from surfaces.config import surface_config

        state: dict = {}
        server = await _gateway(state)
        handed: dict = {}

        async def _run_bot() -> None:
            return None

        try:
            _env(monkeypatch, server, "ada-uuid")
            monkeypatch.setenv("SURFACE_CLAIM_ADOPT_REF", "8776938611")
            monkeypatch.setattr(
                claims, "_configured_list",
                lambda surface: [8776938611, 8636028257],
            )
            monkeypatch.setattr(entry, "run_telegram_bot", _run_bot)
            monkeypatch.setattr(
                entry, "configure",
                lambda cfg: handed.update(users=list(cfg.telegram_allowed_users)),
            )

            await entry._run()
        finally:
            await server.close()

        assert handed["users"] == [8776938611]
        # And the unnamed call still hands over the file's own list, which is
        # what every other reader of this config expects.
        assert list(surface_config().telegram_allowed_users) == [
            8776938611, 8636028257
        ]


class TestAGatewayThatHasDecided:
    async def test_a_decided_claim_stops_the_asking(self, monkeypatch) -> None:
        """Expected on every boot after the first. Treating it as retryable
        held every surface for the whole retry window before it started."""
        state: dict = {"claimed_ref": "8776938611"}
        slept: list[float] = []

        async def _sleep(seconds: float) -> None:
            slept.append(seconds)

        server = await _gateway(state)
        try:
            _env(monkeypatch, server, "bob-uuid")
            monkeypatch.setenv("SURFACE_CLAIM_ADOPT_REF", "8776938611")
            claims.install_claim_resolver("telegram")

            outcome = await claims.adopt_claim("telegram", sleep=_sleep)
        finally:
            await server.close()

        assert outcome == "already_decided"
        assert len(state["adopts"]) == 1
        assert slept == []


class TestAnAdoptionReferenceThatIsNotAnId:
    def test_a_handle_rather_than_an_id_leaves_the_list_alone(
        self, monkeypatch
    ) -> None:
        """Narrowing to something no inbound user can equal empties both
        authorities at once, and the bot goes dark on the first blip."""
        monkeypatch.setenv("SURFACE_CLAIM_ADOPT_REF", "@danielstewart")
        monkeypatch.setattr(
            claims, "_configured_list", lambda surface: [8776938611, 8636028257]
        )

        assert claims.fallback_list("telegram") == [8776938611, 8636028257]

    def test_a_fullwidth_id_is_not_quietly_normalised(self, monkeypatch) -> None:
        """`int` accepts fullwidth digits and `is_allowed` compares text, so
        normalising here produced a fallback that looked right beside a claim
        that could never match."""
        monkeypatch.setenv("SURFACE_CLAIM_ADOPT_REF", "８７７６９３８６１１")
        monkeypatch.setattr(claims, "_configured_list", lambda surface: [])

        assert claims.fallback_list("telegram") == []

    def test_a_circled_numeral_does_not_crash_the_container(
        self, monkeypatch
    ) -> None:
        """`str.isdigit` is true of it and `int` raises on it, inside
        `surface_config` at container start."""
        monkeypatch.setenv("SURFACE_CLAIM_ADOPT_REF", "①")
        monkeypatch.setattr(claims, "_configured_list", lambda surface: [111])

        assert claims.fallback_list("telegram") == [111]

    def test_a_reference_the_list_never_held_narrows_to_it(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("SURFACE_CLAIM_ADOPT_REF", "5550001")
        monkeypatch.setattr(claims, "_configured_list", lambda surface: [111, 222])

        assert claims.fallback_list("telegram") == [5550001]
