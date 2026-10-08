"""Which mind the voice server is asked to speak as.

The voice server resolves a mind's chosen voice from the identifier the surface
sends. Send one nothing can resolve and every mind speaks in the server's
fallback voice, with the console reporting the operator's choice saved — which
is exactly what the containerised stack did, because each surface container
holds `MIND_ID` and no `MIND_NAME`.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import hive_surfaces.telegram_bot as tb


class TestTheVoiceIdItSends:
    def test_is_the_gateways_own_mind_id(self, monkeypatch):
        """The same identifier the Discord surface already sends."""
        monkeypatch.setattr(tb, "gateway", MagicMock(mind_id="41984804-cypher"))
        monkeypatch.delenv("MIND_NAME", raising=False)

        assert tb._voice_id() == "41984804-cypher"

    def test_prefers_the_gateway_over_the_environment(self, monkeypatch):
        """A container inheriting another mind's name must not speak as it."""
        monkeypatch.setattr(tb, "gateway", MagicMock(mind_id="41984804-cypher"))
        monkeypatch.setenv("MIND_NAME", "someone-else")

        assert tb._voice_id() == "41984804-cypher"

    def test_falls_back_to_the_minds_name_before_its_id(self, monkeypatch):
        monkeypatch.setattr(tb, "gateway", None)
        monkeypatch.setenv("MIND_NAME", "cypher")
        monkeypatch.setenv("MIND_ID", "41984804-cypher")

        assert tb._voice_id() == "cypher"

    def test_falls_back_to_the_minds_id_when_it_has_no_name(self, monkeypatch):
        """The containerised surfaces hold exactly this and nothing else."""
        monkeypatch.setattr(tb, "gateway", None)
        monkeypatch.delenv("MIND_NAME", raising=False)
        monkeypatch.setenv("MIND_ID", "41984804-cypher")

        assert tb._voice_id() == "41984804-cypher"


class TestWhatReachesTheVoiceServer:
    def test_every_spoken_reply_carries_that_identity(self, monkeypatch):
        """Asserted on the request that went out, not on the helper alone."""
        import asyncio

        sent = {}

        class _Response:
            status = 200

            async def read(self):
                return b"OggS-audio"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class _Session:
            def post(self, url, json=None):
                sent["url"] = url
                sent["json"] = json
                return _Response()

        monkeypatch.setattr(tb, "http", _Session())
        monkeypatch.setattr(tb, "gateway", MagicMock(mind_id="41984804-cypher"))

        audio = asyncio.run(tb._tts("Hello Daniel"))

        assert audio == b"OggS-audio"
        assert sent["json"] == {"text": "Hello Daniel", "voice_id": "41984804-cypher"}
        assert sent["url"].endswith("/tts")


class TestWhichServerASurfaceCalls:
    """The engine comes off the mind's record, not off this process's env.

    Both bots read a `VOICE_SERVER_URL` of their own and used it for every
    mind they spoke for. Nothing exercised the call sites, so reverting both
    at once left the whole suite green.
    """

    def _resolver(self, rows, fallback=""):
        from hive_surfaces import voice_routing

        return voice_routing.VoiceServerResolver(
            "http://comms:8426",
            "token",
            {
                voice_routing.CHATTERBOX: "http://voice-server:8422",
                voice_routing.KOKORO: "http://voice-server-kokoro:8422",
            },
            fallback_url=fallback,
            fetch=lambda url, token, timeout: rows,
        )

    def test_telegram_asks_the_server_the_minds_record_names(self, monkeypatch):
        monkeypatch.setattr(tb, "gateway", MagicMock(mind_id="ada-uuid"))
        monkeypatch.setattr(
            tb,
            "_voice_servers",
            self._resolver([{"id": "ada-uuid", "voice_engine": "kokoro"}]),
        )

        assert tb._voice_server() == "http://voice-server-kokoro:8422"

    def test_telegram_keeps_its_own_url_for_a_mind_naming_no_engine(
        self, monkeypatch
    ):
        monkeypatch.setattr(tb, "gateway", MagicMock(mind_id="bilby-uuid"))
        monkeypatch.setattr(
            tb,
            "_voice_servers",
            self._resolver(
                [{"id": "bilby-uuid"}], fallback="http://voice-server-kokoro:8422"
            ),
        )

        assert tb._voice_server() == "http://voice-server-kokoro:8422"

    def test_telegram_speaks_through_the_resolved_server(self, monkeypatch):
        """The URL the handler actually posts to, not just what resolves."""
        import asyncio

        monkeypatch.setattr(tb, "gateway", MagicMock(mind_id="ada-uuid"))
        monkeypatch.setattr(
            tb,
            "_voice_servers",
            self._resolver([{"id": "ada-uuid", "voice_engine": "kokoro"}]),
        )
        posted = {}

        class _Post:
            def __init__(self, url, **kwargs):
                posted["url"] = url
                posted["json"] = kwargs.get("json")

            async def __aenter__(self):
                response = MagicMock()
                response.status = 200

                async def read():
                    return b"OggS"

                response.read = read
                return response

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(tb, "http", MagicMock(post=_Post))

        assert asyncio.run(tb._tts("hello")) == b"OggS"
        assert posted["url"] == "http://voice-server-kokoro:8422/tts"
        assert posted["json"]["voice_id"] == "ada-uuid"

    def test_discord_speaks_through_the_resolved_server(self, monkeypatch):
        import asyncio

        import hive_surfaces.discord_bot as db

        monkeypatch.setattr(
            db,
            "_voice_servers",
            self._resolver([{"id": "ada-uuid", "voice_engine": "kokoro"}]),
        )
        posted = {}

        class _Post:
            def __init__(self, url, **kwargs):
                posted["url"] = url

            async def __aenter__(self):
                response = MagicMock()
                response.status = 200

                async def read():
                    return b"OggS"

                response.read = read
                return response

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(db, "http", MagicMock(post=_Post))

        assert asyncio.run(db._tts("hello", voice_id="ada-uuid")) == b"OggS"
        assert posted["url"] == "http://voice-server-kokoro:8422/tts"
