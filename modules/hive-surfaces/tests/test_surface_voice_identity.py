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
