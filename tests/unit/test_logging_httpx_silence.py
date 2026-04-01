"""Test that httpx logger is silenced at WARNING or higher after server import."""

import logging


class TestHttpxLoggerSilenced:
    """Verify httpx logger is set to WARNING to silence Telegram polling noise."""

    def test_httpx_logger_level_is_warning_or_higher(self):
        """After importing server, httpx logger must be WARNING+ to suppress INFO polls."""
        import server  # noqa: F401

        httpx_logger = logging.getLogger("httpx")
        assert httpx_logger.level >= logging.WARNING, (
            f"httpx logger level is {httpx_logger.level}, expected >= {logging.WARNING}"
        )
