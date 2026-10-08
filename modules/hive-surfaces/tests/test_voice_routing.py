"""A mind's speech engine decides which voice server speaks for it.

The engine used to be whichever URL a caller held in its own environment, so
the same mind was spoken by different servers depending on which surface
answered. It is a property of the mind now, read from the record every caller
already asks for the voice name.
"""

from __future__ import annotations

import pytest

from hive_surfaces import voice_routing


KOKORO_URL = "http://voice-server-kokoro:8422"
CHATTERBOX_URL = "http://voice-server:8422"
LEGACY_URL = "http://192.168.4.64:8422"


def _resolver(rows, *, urls=None, fallback=""):
    """A resolver over a fixed broker listing."""
    calls = []

    def fetch(url, token, timeout):
        calls.append((url, token))
        return rows

    resolver = voice_routing.VoiceServerResolver(
        "http://comms:8426",
        "service-token",
        urls if urls is not None else {
            voice_routing.KOKORO: KOKORO_URL,
            voice_routing.CHATTERBOX: CHATTERBOX_URL,
        },
        fallback_url=fallback,
        fetch=fetch,
    )
    return resolver, calls


class TestResolvingTheServer:
    def test_a_kokoro_mind_resolves_to_the_kokoro_server(self):
        """Test 13."""
        resolver, _ = _resolver([{"name": "ada", "voice_engine": "kokoro"}])
        assert resolver.resolve("ada") == KOKORO_URL

    def test_a_chatterbox_mind_resolves_to_the_chatterbox_server(self):
        """Test 14."""
        resolver, _ = _resolver([{"name": "skippy", "voice_engine": "chatterbox"}])
        assert resolver.resolve("skippy") == CHATTERBOX_URL

    def test_a_mind_naming_no_engine_is_described_as_chatterbox(self):
        """Test 15: what the voice server itself has always defaulted to."""
        resolver, _ = _resolver([{"name": "skippy"}])
        assert resolver.engine("skippy") == "chatterbox"
        assert resolver.engine_named("skippy") == ""

    def test_a_mind_naming_no_engine_keeps_the_server_its_caller_holds(self):
        """Declaring nothing means nobody has moved this mind.

        Two minds on this hive were deliberately pointed at the Kokoro server
        by their own `VOICE_SERVER_URL` and name no engine. Resolving them to
        the default would move one onto a cloned chatterbox voice and silence
        the other, which is the opposite of what adding a field should do.
        """
        resolver, _ = _resolver([{"name": "bilby"}], fallback=KOKORO_URL)
        assert resolver.resolve("bilby") == KOKORO_URL

    def test_a_mind_that_names_an_engine_leaves_the_callers_server_behind(self):
        """Naming one is the act that moves a mind; that is the whole point."""
        resolver, _ = _resolver(
            [{"name": "bilby", "voice_engine": "chatterbox"}], fallback=KOKORO_URL
        )
        assert resolver.resolve("bilby") == CHATTERBOX_URL

    def test_a_mind_the_gateway_has_never_heard_of_keeps_the_callers_server(self):
        resolver, _ = _resolver([], fallback=KOKORO_URL)
        assert resolver.resolve("stranger") == KOKORO_URL

    def test_with_no_caller_server_an_undeclared_mind_falls_to_chatterbox(self):
        """What the voice server itself defaults to, when nothing else says."""
        resolver, _ = _resolver([{"name": "skippy"}])
        assert resolver.resolve("skippy") == CHATTERBOX_URL

    def test_an_engine_this_build_does_not_know_is_not_followed(self):
        """A URL cannot be guessed from a name the build has no entry for."""
        resolver, _ = _resolver([{"name": "ada", "voice_engine": "festival"}])
        assert resolver.engine("ada") == "chatterbox"
        assert resolver.engine_named("ada") == ""

    def test_a_mind_addressed_by_uuid_resolves_the_same_as_by_name(self):
        """Test 17: the surfaces send a UUID, a person types a short name."""
        rows = [
            {
                "name": "skippy",
                "id": "14cb820b-4a42-4f04-a593-54f532fd1d2f",
                "voice_engine": "kokoro",
            }
        ]
        resolver, _ = _resolver(rows)
        assert resolver.resolve("14cb820b-4a42-4f04-a593-54f532fd1d2f") == KOKORO_URL
        assert resolver.resolve("skippy") == KOKORO_URL


class TestASingleServerHost:
    def test_the_legacy_url_answers_for_either_engine(self):
        """Test 16: a host nobody reconfigures behaves exactly as it did."""
        resolver, _ = _resolver(
            [{"name": "ada", "voice_engine": "kokoro"}], urls={}, fallback=LEGACY_URL
        )
        assert resolver.resolve("ada") == LEGACY_URL
        assert resolver.resolve("skippy") == LEGACY_URL

    def test_a_configured_engine_url_wins_over_the_fallback(self):
        resolver, _ = _resolver(
            [{"name": "ada", "voice_engine": "kokoro"}],
            urls={voice_routing.KOKORO: KOKORO_URL},
            fallback=LEGACY_URL,
        )
        assert resolver.resolve("ada") == KOKORO_URL
        # Chatterbox has no URL of its own here, so it takes the one server
        # this host actually has.
        assert resolver.resolve("skippy") == LEGACY_URL

    def test_nothing_configured_resolves_to_nothing(self):
        resolver, _ = _resolver([{"name": "ada"}], urls={})
        assert resolver.resolve("ada") == ""


class TestTheListing:
    def test_the_listing_is_reused_within_its_ttl(self):
        """One sentence of speech must not cost a round trip per call."""
        resolver, calls = _resolver([{"name": "ada", "voice_engine": "kokoro"}])
        resolver.resolve("ada")
        resolver.resolve("ada")
        assert len(calls) == 1

    def test_an_unreachable_gateway_still_answers(self):
        """A gateway that is down costs a wrong voice, never a lost reply."""

        def fetch(url, token, timeout):
            raise OSError("comms is down")

        resolver = voice_routing.VoiceServerResolver(
            "http://comms:8426",
            "token",
            {voice_routing.CHATTERBOX: CHATTERBOX_URL},
            fetch=fetch,
        )
        assert resolver.resolve("ada") == CHATTERBOX_URL

    def test_the_listing_is_read_from_the_broker_with_the_service_token(self):
        resolver, calls = _resolver([{"name": "ada", "voice_engine": "kokoro"}])
        resolver.resolve("ada")
        assert calls == [("http://comms:8426", "service-token")]

    def test_a_resolver_built_without_a_fetch_uses_the_broker_listing(self):
        """The injected `fetch` in every other test must not be the only
        implementation anything ever reaches."""
        resolver = voice_routing.VoiceServerResolver(
            "http://comms:8426", "t", {voice_routing.KOKORO: KOKORO_URL}
        )
        assert resolver._fetch is voice_routing._fetch_minds



class TestFetchingTheListing:
    """`_fetch_minds` itself, not a stand-in for it."""

    def _urlopen(self, monkeypatch, body):
        import json as _json

        seen = {}

        class _Response:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner):
                return _json.dumps(body).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["headers"] = dict(request.header_items())
            seen["timeout"] = timeout
            return _Response()

        monkeypatch.setattr(
            voice_routing.urllib.request, "urlopen", fake_urlopen
        )
        return seen

    def test_it_asks_the_broker_with_the_service_token(self, monkeypatch):
        seen = self._urlopen(
            monkeypatch, [{"name": "ada", "voice_engine": "kokoro"}]
        )

        rows = voice_routing._fetch_minds("http://comms:8426/", "svc-token", 3.0)

        assert seen["url"] == "http://comms:8426/broker/minds"
        assert seen["headers"]["Authorization"] == "Bearer svc-token"
        assert seen["timeout"] == 3.0
        assert rows == [{"name": "ada", "voice_engine": "kokoro"}]

    def test_it_reads_a_dict_bodied_listing(self, monkeypatch):
        """The gateway answers a bare list today and may wrap it tomorrow."""
        self._urlopen(monkeypatch, {"minds": [{"name": "ada"}]})

        assert voice_routing._fetch_minds("http://comms:8426", "t", 3.0) == [
            {"name": "ada"}
        ]

    def test_it_drops_entries_that_are_not_rows(self, monkeypatch):
        self._urlopen(monkeypatch, ["not-a-row", {"name": "ada"}])

        assert voice_routing._fetch_minds("http://comms:8426", "t", 3.0) == [
            {"name": "ada"}
        ]

    def test_it_sends_no_authorization_when_there_is_no_token(self, monkeypatch):
        seen = self._urlopen(monkeypatch, [])

        voice_routing._fetch_minds("http://comms:8426", "", 3.0)

        assert "Authorization" not in seen["headers"]


class TestIndexing:
    def test_both_keys_index_one_row(self):
        index = voice_routing.engine_index(
            [{"name": "ada", "mind_id": "uuid-1", "voice_engine": "kokoro"}]
        )
        assert index == {"ada": "kokoro", "uuid-1": "kokoro"}

    def test_a_row_naming_no_engine_contributes_nothing(self):
        assert voice_routing.engine_index([{"name": "ada"}]) == {}

    @pytest.mark.parametrize("engine", ["KOKORO", " Kokoro "])
    def test_an_engine_is_matched_regardless_of_case_or_padding(self, engine):
        index = voice_routing.engine_index([{"name": "ada", "voice_engine": engine}])
        assert index["ada"] == "kokoro"
