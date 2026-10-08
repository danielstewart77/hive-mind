"""A mind's voice is read from the mind's own record, not this container's env.

`KOKORO_VOICE_MAP` is a JSON blob in the voice server's environment, loaded once
at startup, so changing one mind's voice meant restarting a container that
reloads a speech model. The record is the truth now; the map is only a fallback
for a mind whose code predates the field.
"""

from __future__ import annotations

from voice import mind_voices
from voice.mind_voices import MindVoiceResolver, voice_index

ROWS = [
    {"name": "cypher", "id": "41984804-cypher", "voice": "bm_george"},
    {"name": "bob", "id": "u-bob", "voice": ""},
    {"name": "ada", "id": "u-ada"},
]


def resolver(rows=ROWS, **kwargs):
    kwargs.setdefault("fetch", lambda url, token, timeout: rows)
    return MindVoiceResolver("http://comms", "service-token", **kwargs)


class TestIndex:
    def test_a_mind_is_addressable_by_name_and_by_uuid(self):
        """The surfaces send a UUID; a person typing sends the short name."""
        index = voice_index(ROWS)
        assert index["cypher"] == "bm_george"
        assert index["41984804-cypher"] == "bm_george"

    def test_a_mind_with_no_voice_contributes_nothing(self):
        """An empty string would resolve to the empty voice, not the default."""
        assert "bob" not in voice_index(ROWS)
        assert "ada" not in voice_index(ROWS)


class TestResolution:
    def test_the_record_is_what_a_mind_is_spoken_in(self):
        assert resolver().resolve("cypher", env_map={}, default="af_heart") == "bm_george"

    def test_the_record_wins_over_the_legacy_environment_map(self):
        """Otherwise a stale env entry silently outranks a console change."""
        assert (
            resolver().resolve(
                "cypher", env_map={"cypher": "am_michael"}, default="af_heart"
            )
            == "bm_george"
        )

    def test_the_environment_map_still_answers_for_a_mind_with_no_record(self):
        assert (
            resolver().resolve("bob", env_map={"bob": "am_michael"}, default="af_heart")
            == "am_michael"
        )

    def test_a_mind_that_has_picked_nothing_gets_the_default(self):
        assert resolver().resolve("ada", env_map={}, default="af_heart") == "af_heart"

    def test_an_unreachable_gateway_falls_back_rather_than_raising(self):
        """One sentence in the wrong voice beats every reply failing."""

        def explode(url, token, timeout):
            raise OSError("connection refused")

        assert (
            resolver(fetch=explode).resolve(
                "cypher", env_map={"cypher": "am_michael"}, default="af_heart"
            )
            == "am_michael"
        )

    def test_the_listing_is_reused_inside_the_cache_window(self):
        """A spoken sentence must not cost an HTTP round trip per call."""
        calls = []

        def counting_fetch(url, token, timeout):
            calls.append(1)
            return ROWS

        now = [1000.0]
        r = MindVoiceResolver(
            "http://comms", "t", fetch=counting_fetch, ttl_seconds=30.0,
            clock=lambda: now[0],
        )
        r.resolve("cypher", env_map={}, default="af_heart")
        now[0] += 29.0
        r.resolve("cypher", env_map={}, default="af_heart")
        assert len(calls) == 1
        now[0] += 2.0
        r.resolve("cypher", env_map={}, default="af_heart")
        assert len(calls) == 2


class TestTheNameIndex:
    """`{uuid -> short name}`, for a caller holding only a UUID.

    The voice server's on-disk scan can only map minds whose `runtime.yaml`
    lives under its own `minds/`, which excludes every edge install. The
    gateway's listing is the only thing that knows the rest.
    """

    def test_both_id_spellings_map_to_the_name(self):
        index = mind_voices.name_index(
            [{"name": "skippy", "id": "uuid-1"}, {"name": "ada", "mind_id": "uuid-2"}]
        )
        assert index == {"uuid-1": "skippy", "uuid-2": "ada"}

    def test_a_row_with_no_name_contributes_nothing(self):
        assert mind_voices.name_index([{"id": "uuid-1"}]) == {}

    def test_the_resolver_answers_a_uuid_with_the_short_name(self):
        resolver = MindVoiceResolver(
            "http://comms:8426",
            "token",
            fetch=lambda url, token, timeout: [
                {"name": "skippy", "id": "14cb820b", "voice": "voice_ref.wav"}
            ],
        )
        assert resolver.short_name("14cb820b") == "skippy"

    def test_a_uuid_the_gateway_does_not_know_answers_empty(self):
        resolver = MindVoiceResolver(
            "http://comms:8426", "token", fetch=lambda url, token, timeout: []
        )
        assert resolver.short_name("stranger") == ""

    def test_an_unreachable_gateway_answers_empty_rather_than_raising(self):
        def fetch(url, token, timeout):
            raise OSError("comms is down")

        resolver = MindVoiceResolver(
            "http://comms:8426", "token", fetch=fetch
        )
        assert resolver.short_name("14cb820b") == ""
