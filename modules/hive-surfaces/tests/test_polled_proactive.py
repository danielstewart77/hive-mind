"""Unsolicited turns reaching a surface that shares no process with its mind.

An edge mind hands them over in memory. A surface in its own container cannot
be handed anything, so it polls the backend — and puts what it finds on the
same queue, so a polled turn gets the same chunking, backoff and
shutdown journalling as one handed over in process.
"""

import asyncio
import contextlib
from unittest.mock import MagicMock, patch

import pytest

from hive_surfaces import proactive
from hive_surfaces.config import SurfaceConfig, config, configure
import hive_surfaces.telegram_bot as tb


@pytest.fixture(autouse=True)
def _clean_queue_and_config():
    before = config._installed()
    for _chat, _text, _attempts in proactive.drain():
        pass
    yield
    for _chat, _text, _attempts in proactive.drain():
        pass
    configure(before)


class _Response:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _http_returning(*responses):
    """A session whose GET answers each response in turn, then blocks."""
    it = iter(responses)
    session = MagicMock()

    def _get(*_a, **_kw):
        try:
            return next(it)
        except StopIteration:
            return _Response(200, [])

    session.get = _get
    return session


async def _run_source_until(http_session, *, want: int = 1, timeout: float = 2.0) -> None:
    """Run the poller until `want` items are queued, or the timeout expires.

    Waiting a fixed interval would make the test's verdict depend on how long
    a log write takes on the machine running it.
    """
    with patch.object(tb, "http", http_session):
        task = asyncio.ensure_future(tb._proactive_poll_source())
        deadline = asyncio.get_running_loop().time() + timeout
        while proactive.pending() < want and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TestWhatPollingPutsOnTheQueue:
    async def test_a_polled_turn_lands_on_the_delivery_queue(self) -> None:
        configure(SurfaceConfig(
            proactive_poll_url="http://mind.example:8420",
            proactive_poll_interval_s=0.01,
        ))

        await _run_source_until(_http_returning(
            _Response(200, [{"chat_id": 4242, "text": "the answer you never saw"}]),
        ))

        queued = [(c, t) for c, t, _a in proactive.drain()]
        assert (4242, "the answer you never saw") in queued

    async def test_a_chat_id_arriving_as_a_string_still_routes(self) -> None:
        """JSON hands back whatever the backend serialized."""
        configure(SurfaceConfig(
            proactive_poll_url="http://mind.example:8420",
            proactive_poll_interval_s=0.01,
        ))

        await _run_source_until(_http_returning(
            _Response(200, [{"chat_id": "4242", "text": "hello"}]),
        ))

        assert [c for c, _t, _a in proactive.drain()] == [4242]

    async def test_a_turn_with_no_destination_is_not_queued(self) -> None:
        configure(SurfaceConfig(
            proactive_poll_url="http://mind.example:8420",
            proactive_poll_interval_s=0.01,
        ))

        await _run_source_until(_http_returning(
            _Response(200, [{"chat_id": None, "text": "nowhere to go"}]),
        ), want=1, timeout=0.2)

        assert list(proactive.drain()) == []


class TestThePollerSurvivesABadBackend:
    async def test_a_rejected_poll_does_not_end_the_loop(self) -> None:
        """A healthy conversation must not depend on a healthy proactive channel."""
        configure(SurfaceConfig(
            proactive_poll_url="http://mind.example:8420",
            proactive_poll_interval_s=0.01,
        ))

        await _run_source_until(_http_returning(
            _Response(503, []),
            _Response(200, [{"chat_id": 7, "text": "after the outage"}]),
        ))

        assert [c for c, _t, _a in proactive.drain()] == [7]

    async def test_a_raising_session_does_not_end_the_loop(self) -> None:
        configure(SurfaceConfig(
            proactive_poll_url="http://mind.example:8420",
            proactive_poll_interval_s=0.01,
        ))

        session = MagicMock()
        calls = {"n": 0}

        def _get(*_a, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("connection refused")
            return _Response(200, [{"chat_id": 9, "text": "recovered"}])

        session.get = _get
        await _run_source_until(session)

        assert [c for c, _t, _a in proactive.drain()] == [9]


class TestWhetherPollingRunsAtAll:
    def test_an_edge_mind_configures_no_poll_url(self) -> None:
        """In-process handover is the default; polling is opt-in."""
        configure(SurfaceConfig())

        assert config.proactive_poll_url == ""
