"""Unit tests for tool_creator.py audit logger migration.

Verifies that tool_creator uses the shared audit logger from core.audit
with RotatingFileHandler instead of a plain FileHandler.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler


class TestToolCreatorAudit:
    """Tests that tool_creator uses the shared RotatingFileHandler-based logger."""

    def _get_fresh_audit_logger(self) -> logging.Logger:
        """Clear the hive_mind.audit logger and re-import tool_creator to get a fresh setup."""
        # Clear any existing handlers on the shared logger
        logger = logging.getLogger("hive_mind.audit")
        logger.handlers.clear()

        # Remove cached modules so they re-execute module-level code
        sys.modules.pop("agents.tool_creator", None)
        sys.modules.pop("core.audit", None)

        # Re-import to trigger fresh setup
        from agents.tool_creator import _audit

        return _audit

    def test_tool_creator_uses_shared_logger(self) -> None:
        """The _audit logger should use RotatingFileHandler, not plain FileHandler."""
        audit = self._get_fresh_audit_logger()
        rotating_handlers = [
            h for h in audit.handlers if isinstance(h, RotatingFileHandler)
        ]
        assert len(rotating_handlers) >= 1, (
            f"Expected at least one RotatingFileHandler, "
            f"got handlers: {[type(h).__name__ for h in audit.handlers]}"
        )

    def test_tool_creator_logger_name(self) -> None:
        """The _audit logger should be named hive_mind.audit."""
        audit = self._get_fresh_audit_logger()
        assert audit.name == "hive_mind.audit"

    def test_tool_creator_no_plain_file_handler(self) -> None:
        """There should be no plain FileHandler (only Rotating ones)."""
        audit = self._get_fresh_audit_logger()
        plain_handlers = [
            h
            for h in audit.handlers
            if type(h) is logging.FileHandler  # exact type, not subclass
        ]
        assert len(plain_handlers) == 0, (
            f"Found plain FileHandler(s) that should have been replaced: "
            f"{plain_handlers}"
        )
