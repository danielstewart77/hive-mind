"""Unit tests for structured logging in core/sessions.py — send_message lifecycle."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def _mock_sessions_deps(monkeypatch):
    """Suppress heavy deps (aiosqlite, config, etc.) to allow import."""
    # Already handled by conftest for keyring/neo4j/requests, but we need
    # to ensure config module is importable.
    pass


class TestSendMessageLogging:
    """Verify send_message emits structured logs at lifecycle boundaries."""

    @pytest.mark.asyncio
    async def test_send_message_logs_start(self):
        """send_message must log INFO 'send_message: start' with session= and mind=."""
        with patch("core.sessions.log") as mock_log:
            from core.sessions import SessionManager

            mgr = SessionManager.__new__(SessionManager)
            mgr._db = AsyncMock()
            mgr._procs = {}
            mgr._mind_ids = {}
            mgr._locks = {}
            mgr._registry = MagicMock()

            # Mock _get_row to return a session
            fake_session = {
                "mind_id": "ada",
                "model": "sonnet",
                "autopilot": 0,
                "claude_sid": None,
                "summary": "Test session",
            }
            mgr._get_row = AsyncMock(return_value=fake_session)

            # Mock _spawn to register a proc
            async def fake_spawn(*args, **kwargs):
                mgr._procs["sess-1"] = MagicMock(returncode=None)

            mgr._spawn = AsyncMock(side_effect=fake_spawn)

            # Mock _load_implementation to return an SDK impl with send
            mock_impl = MagicMock()

            async def fake_send(session_id, content, **kwargs):
                yield {"type": "result", "result": "ok"}

            mock_impl.send = fake_send

            with patch("core.sessions._load_implementation", return_value=mock_impl):
                events = []
                async for event in mgr.send_message("sess-1", "hello"):
                    events.append(event)

            # Check that log.info was called with "send_message: start"
            start_calls = [
                c for c in mock_log.info.call_args_list
                if len(c.args) > 0 and "send_message: start" in str(c.args[0])
            ]
            assert len(start_calls) >= 1, (
                f"Expected 'send_message: start' log, got: {mock_log.info.call_args_list}"
            )
            # Verify session= and mind= are in the format string args
            call_str = str(start_calls[0])
            assert "sess-1" in call_str or "session=" in str(start_calls[0].args[0])

    @pytest.mark.asyncio
    async def test_send_message_logs_respawn_when_proc_missing(self):
        """send_message must log INFO 'send_message: respawn' when session not in _procs."""
        with patch("core.sessions.log") as mock_log:
            from core.sessions import SessionManager

            mgr = SessionManager.__new__(SessionManager)
            mgr._db = AsyncMock()
            mgr._procs = {}  # Empty — will trigger respawn
            mgr._mind_ids = {}
            mgr._locks = {}
            mgr._registry = MagicMock()

            fake_session = {
                "mind_id": "bob",
                "model": "sonnet",
                "autopilot": 0,
                "claude_sid": "old-sid",
                "summary": "Test session",
            }
            mgr._get_row = AsyncMock(return_value=fake_session)

            async def fake_spawn(*args, **kwargs):
                mgr._procs["sess-2"] = MagicMock(returncode=None)

            mgr._spawn = AsyncMock(side_effect=fake_spawn)

            mock_impl = MagicMock()

            async def fake_send(session_id, content, **kwargs):
                yield {"type": "result", "result": "ok"}

            mock_impl.send = fake_send

            with patch("core.sessions._load_implementation", return_value=mock_impl):
                events = []
                async for event in mgr.send_message("sess-2", "hello"):
                    events.append(event)

            # Check for respawn log
            respawn_calls = [
                c for c in mock_log.info.call_args_list
                if len(c.args) > 0 and "send_message: respawn" in str(c.args[0])
            ]
            assert len(respawn_calls) >= 1, (
                f"Expected 'send_message: respawn' log, got: {mock_log.info.call_args_list}"
            )

    @pytest.mark.asyncio
    async def test_send_message_logs_result_with_elapsed(self):
        """send_message must log INFO 'send_message: result' with elapsed= after completion."""
        with patch("core.sessions.log") as mock_log:
            from core.sessions import SessionManager

            mgr = SessionManager.__new__(SessionManager)
            mgr._db = AsyncMock()
            mgr._procs = {"sess-3": MagicMock(returncode=None)}
            mgr._mind_ids = {"sess-3": "ada"}
            mgr._locks = {}
            mgr._registry = MagicMock()

            fake_session = {
                "mind_id": "ada",
                "model": "sonnet",
                "autopilot": 0,
                "claude_sid": None,
                "summary": "Existing session",
            }
            mgr._get_row = AsyncMock(return_value=fake_session)

            mock_impl = MagicMock()

            async def fake_send(session_id, content, **kwargs):
                yield {"type": "result", "result": "ok"}

            mock_impl.send = fake_send

            with patch("core.sessions._load_implementation", return_value=mock_impl):
                events = []
                async for event in mgr.send_message("sess-3", "hello"):
                    events.append(event)

            result_calls = [
                c for c in mock_log.info.call_args_list
                if len(c.args) > 0 and "send_message: result" in str(c.args[0])
                and "elapsed=" in str(c.args[0])
            ]
            assert len(result_calls) >= 1, (
                f"Expected 'send_message: result' with 'elapsed=', got: {mock_log.info.call_args_list}"
            )

    @pytest.mark.asyncio
    async def test_send_message_logs_slow_response_warning(self):
        """send_message must log WARNING 'send_message: slow response' when elapsed > 30s."""
        with patch("core.sessions.log") as mock_log:
            from core.sessions import SessionManager

            mgr = SessionManager.__new__(SessionManager)
            mgr._db = AsyncMock()
            mgr._procs = {"sess-4": MagicMock(returncode=None)}
            mgr._mind_ids = {"sess-4": "bob"}
            mgr._locks = {}
            mgr._registry = MagicMock()

            fake_session = {
                "mind_id": "bob",
                "model": "sonnet",
                "autopilot": 0,
                "claude_sid": None,
                "summary": "Existing session",
            }
            mgr._get_row = AsyncMock(return_value=fake_session)

            mock_impl = MagicMock()

            async def fake_send(session_id, content, **kwargs):
                yield {"type": "result", "result": "ok"}

            mock_impl.send = fake_send

            # Mock time.monotonic to simulate >30s elapsed
            call_count = 0

            def fake_monotonic():
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return 100.0  # start time (t0)
                return 145.0  # 45s later — >30s threshold

            with (
                patch("core.sessions._load_implementation", return_value=mock_impl),
                patch("core.sessions.time") as mock_time,
            ):
                mock_time.monotonic = fake_monotonic
                mock_time.time = time.time  # keep time.time() working

                events = []
                async for event in mgr.send_message("sess-4", "hello"):
                    events.append(event)

            warning_calls = [
                c for c in mock_log.warning.call_args_list
                if len(c.args) > 0 and "send_message: slow response" in str(c.args[0])
            ]
            assert len(warning_calls) >= 1, (
                f"Expected 'send_message: slow response' warning, got: {mock_log.warning.call_args_list}"
            )

    @pytest.mark.asyncio
    async def test_spawn_logs_with_session_mind_model(self):
        """_spawn log line must include session_id, mind_id, and model."""
        with patch("core.sessions.log") as mock_log:
            from core.sessions import SessionManager

            mgr = SessionManager.__new__(SessionManager)
            mgr._db = AsyncMock()
            mgr._procs = {}
            mgr._mind_ids = {}
            mgr._locks = {}
            mgr._registry = MagicMock()

            mock_impl = MagicMock()
            mock_impl.spawn = AsyncMock(return_value=MagicMock())

            with patch("core.sessions._load_implementation", return_value=mock_impl):
                await mgr._spawn("sess-5", "sonnet", mind_id="nagatha")

            spawn_calls = [
                c for c in mock_log.info.call_args_list
                if len(c.args) > 0 and "Spawned" in str(c.args[0]) or "spawn" in str(c.args[0]).lower()
            ]
            assert len(spawn_calls) >= 1, (
                f"Expected spawn log with session/mind/model, got: {mock_log.info.call_args_list}"
            )
            # Verify the args contain session_id, mind_id, model
            call_str = str(spawn_calls[0])
            assert "sess-5" in call_str
            assert "nagatha" in call_str
            assert "sonnet" in call_str


class _AsyncLineIter:
    """Helper that wraps a list of byte lines into an async iterator."""

    def __init__(self, lines: list[bytes]):
        self._lines = lines
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._index]
        self._index += 1
        return line


class TestStderrDrainLogging:
    """Verify that subprocess stderr lines are logged at WARNING level."""

    @pytest.mark.asyncio
    async def test_stderr_logged_at_warning(self):
        """When a CLI subprocess emits stderr, _drain_stderr logs it at WARNING."""
        with patch("core.sessions.log") as mock_log:
            from core.sessions import _drain_stderr

            # Build a mock process whose stderr yields one line then stops
            mock_proc = MagicMock()
            mock_proc.stderr = _AsyncLineIter([b"Error: something went wrong\n"])

            await _drain_stderr(mock_proc, "sess-stderr-1")

            # Assert log.warning was called with "subprocess stderr:" and session id
            warning_calls = [
                c for c in mock_log.warning.call_args_list
                if len(c.args) > 0 and "subprocess stderr:" in str(c.args[0])
            ]
            assert len(warning_calls) >= 1, (
                f"Expected 'subprocess stderr:' warning, got: {mock_log.warning.call_args_list}"
            )
            call_str = str(warning_calls[0])
            assert "sess-stderr-1" in call_str
            assert "something went wrong" in call_str

    @pytest.mark.asyncio
    async def test_stderr_drain_skips_empty_lines(self):
        """Empty stderr lines should not produce warning logs."""
        with patch("core.sessions.log") as mock_log:
            from core.sessions import _drain_stderr

            mock_proc = MagicMock()
            mock_proc.stderr = _AsyncLineIter([b"\n", b"   \n"])

            await _drain_stderr(mock_proc, "sess-empty")

            warning_calls = [
                c for c in mock_log.warning.call_args_list
                if len(c.args) > 0 and "subprocess stderr:" in str(c.args[0])
            ]
            assert len(warning_calls) == 0, (
                f"Expected no warnings for empty stderr lines, got: {mock_log.warning.call_args_list}"
            )

    @pytest.mark.asyncio
    async def test_stderr_drain_noop_when_stderr_is_none(self):
        """_drain_stderr must be a no-op when proc.stderr is None."""
        with patch("core.sessions.log") as mock_log:
            from core.sessions import _drain_stderr

            mock_proc = MagicMock()
            mock_proc.stderr = None

            await _drain_stderr(mock_proc, "sess-none")

            warning_calls = [
                c for c in mock_log.warning.call_args_list
                if len(c.args) > 0 and "subprocess stderr:" in str(c.args[0])
            ]
            assert len(warning_calls) == 0

    @pytest.mark.asyncio
    async def test_stderr_drain_truncates_long_lines(self):
        """Lines longer than 200 chars should be truncated."""
        with patch("core.sessions.log") as mock_log:
            from core.sessions import _drain_stderr

            mock_proc = MagicMock()
            long_line = "x" * 300
            mock_proc.stderr = _AsyncLineIter([(long_line + "\n").encode()])

            await _drain_stderr(mock_proc, "sess-long")

            warning_calls = [
                c for c in mock_log.warning.call_args_list
                if len(c.args) > 0 and "subprocess stderr:" in str(c.args[0])
            ]
            assert len(warning_calls) == 1
            # The logged line content (3rd positional arg in the format call) should be truncated
            logged_line = warning_calls[0].args[2]
            assert len(logged_line) <= 200
