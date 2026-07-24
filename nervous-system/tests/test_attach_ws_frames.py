"""Attach-WS frame-type preservation.

The pty-attach protocol carries two frame kinds on the browser→mind leg:
BINARY frames are raw terminal bytes, TEXT frames are JSON control
messages (resize). The bridge must forward each with its type intact — a
TEXT control frame re-encoded into the byte stream would be typed into
the TUI instead of resizing the pty.
"""
from __future__ import annotations

import asyncio

import aiohttp

from comms.server import _pump_attach_ws


class _FakeBrowserWS:
    """Starlette-style receive(): yields dicts then a disconnect."""

    def __init__(self, messages):
        self._messages = list(messages)
        self.sent_bytes = []

    async def receive(self):
        if self._messages:
            return self._messages.pop(0)
        return {"type": "websocket.disconnect"}

    async def send_bytes(self, data):
        self.sent_bytes.append(data)


class _FakeMindWS:
    """aiohttp-style client WS: records send types, yields nothing."""

    def __init__(self):
        self.sent = []  # (kind, payload)
        self._closed = asyncio.Event()

    async def send_bytes(self, data):
        self.sent.append(("bytes", data))

    async def send_str(self, data):
        self.sent.append(("str", data))

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self._closed.wait()  # never set — ends when the pump is cancelled
        raise StopAsyncIteration


def test_browser_binary_and_text_frames_keep_their_types():
    browser = _FakeBrowserWS([
        {"type": "websocket.receive", "bytes": b"ls\n"},
        {"type": "websocket.receive", "text": '{"type":"resize","cols":100,"rows":30}'},
        {"type": "websocket.receive", "bytes": b"\x03"},
    ])
    mind = _FakeMindWS()

    asyncio.run(_pump_attach_ws(browser, mind))

    assert mind.sent == [
        ("bytes", b"ls\n"),
        ("str", '{"type":"resize","cols":100,"rows":30}'),
        ("bytes", b"\x03"),
    ]


def test_mind_output_reaches_browser_as_bytes():
    class _Msg:
        def __init__(self, type_, data):
            self.type = type_
            self.data = data

    class _EmittingMindWS(_FakeMindWS):
        def __init__(self):
            super().__init__()
            self._out = [
                _Msg(aiohttp.WSMsgType.BINARY, b"tui bytes"),
                _Msg(aiohttp.WSMsgType.TEXT, "tui text"),
                _Msg(aiohttp.WSMsgType.CLOSE, None),
            ]

        async def __anext__(self):
            if self._out:
                return self._out.pop(0)
            raise StopAsyncIteration

    browser = _FakeBrowserWS([])
    mind = _EmittingMindWS()

    asyncio.run(_pump_attach_ws(browser, mind))

    assert browser.sent_bytes == [b"tui bytes", b"tui text".decode().encode()]
