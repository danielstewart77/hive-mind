"""Every Discord reply arrives as audio beside its text.

Discord's only voice path played into a live voice channel, which a text
channel on a phone does not have — so a reply read on a phone was text or
nothing. The audio is an attachment on the channel instead.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import hive_surfaces.discord_bot as db


class _Response:
    status = 200

    async def read(self):
        return b"OggS-audio"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, sent):
        self._sent = sent

    def post(self, url, json=None):
        self._sent["url"] = url
        self._sent["json"] = json
        return _Response()


class _RefusingSession:
    def post(self, url, json=None):
        raise ConnectionError("voice server down")


class _Channel:
    def __init__(self):
        self.files = []

    async def send(self, file=None, **kwargs):
        self.files.append(file)


class TestTheAudioBesideTheText:
    def test_the_whole_reply_is_synthesised_and_uploaded(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(db, "http", _Session(sent))
        monkeypatch.setattr(db, "gateway", MagicMock(mind_id="14cb820b-skippy"))
        channel = _Channel()

        asyncio.run(db._send_voice_note(channel, "  Two paragraphs of answer.  "))

        assert sent["json"] == {
            "text": "Two paragraphs of answer.",
            "voice_id": "14cb820b-skippy",
        }
        assert sent["url"].endswith("/tts")
        assert len(channel.files) == 1
        assert channel.files[0].fp.read() == b"OggS-audio"

    def test_a_dead_voice_server_costs_the_audio_and_nothing_else(self, monkeypatch):
        monkeypatch.setattr(db, "http", _RefusingSession())
        monkeypatch.setattr(db, "gateway", MagicMock(mind_id="14cb820b-skippy"))
        channel = _Channel()

        asyncio.run(db._send_voice_note(channel, "The text already landed."))

        assert channel.files == []


class TestTheSwitch:
    def test_voice_off_starts_nothing(self, monkeypatch):
        monkeypatch.setattr(db, "ALWAYS_VOICE", False)

        assert db.speak_reply(_Channel(), "A finished reply.") is None

    def test_voice_on_starts_the_upload(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(db, "ALWAYS_VOICE", True)
        monkeypatch.setattr(db, "http", _Session(sent))
        monkeypatch.setattr(db, "gateway", MagicMock(mind_id="14cb820b-skippy"))
        channel = _Channel()

        async def drive():
            task = db.speak_reply(channel, "A finished reply.")
            assert task is not None
            await task

        asyncio.run(drive())

        assert sent["json"]["text"] == "A finished reply."
        assert len(channel.files) == 1
