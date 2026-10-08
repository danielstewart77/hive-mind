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

    def test_a_mind_naming_no_engine_resolves_to_chatterbox(self):
        """Test 15: what the voice server itself has always defaulted to."""
        resolver, _ = _resolver([{"name": "skippy"}])
        assert resolver.engine("skippy") == "chatterbox"
        assert resolver.resolve("skippy") == CHATTERBOX_URL

    def test_a_mind_the_gateway_has_never_heard_of_resolves_to_chatterbox(self):
        resolver, _ = _resolver([])
        assert resolver.resolve("stranger") == CHATTERBOX_URL

    def test_an_engine_this_build_does_not_know_is_not_followed(self):
        """A URL cannot be guessed from a name the build has no entry for."""
        resolver, _ = _resolver([{"name": "ada", "voice_engine": "festival"}])
        assert resolver.engine("ada") == "chatterbox"

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

    def test_a_dict_bodied_listing_is_read_the_same_as_a_list(self):
        resolver = voice_routing.VoiceServerResolver(
            "http://comms:8426",
            "token",
            {voice_routing.KOKORO: KOKORO_URL},
            fetch=lambda u, t, to: [{"name": "ada", "voice_engine": "kokoro"}],
        )
        assert resolver.resolve("ada") == KOKORO_URL


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
