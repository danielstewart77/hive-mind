"""Centralised audit logging for MCP tool invocations.

Provides structured JSON audit logging with secret redaction and log rotation.
All MCP tool calls are wrapped with audit_wrap() to produce a consistent log
trail without leaking sensitive parameter values.
"""

import functools
import inspect
import json
import logging
import os
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Callable

# Parameter names whose values should be redacted from logs
SENSITIVE_PARAMS: frozenset[str] = frozenset(
    {"value", "password", "token", "secret", "auth"}
)

# String arguments longer than this are replaced with a length summary
MAX_ARG_LENGTH: int = 200

# Default log file location
_DEFAULT_LOG_PATH: str = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "audit.log"
)


def redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *args* with sensitive values redacted and long strings truncated.

    - Keys matching SENSITIVE_PARAMS have their values replaced with ``***REDACTED***``.
    - String values longer than MAX_ARG_LENGTH are replaced with ``[N chars]``.
    - The original dict is never mutated.
    """
    redacted: dict[str, Any] = {}
    for key, val in args.items():
        if key in SENSITIVE_PARAMS:
            redacted[key] = "***REDACTED***"
        elif isinstance(val, str) and len(val) > MAX_ARG_LENGTH:
            redacted[key] = f"[{len(val)} chars]"
        else:
            redacted[key] = val
    return redacted


class JSONAuditFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects.

    If the record carries an ``audit_data`` attribute (set via the *extra*
    parameter of ``logger.info``), those fields are merged into the output.
    Otherwise, the plain message is emitted under a ``"message"`` key.
    """

    def format(self, record: logging.LogRecord) -> str:
        audit_data: dict[str, Any] | None = getattr(record, "audit_data", None)
        if audit_data is not None:
            entry = {
                "timestamp": datetime.fromtimestamp(
                    record.created, tz=timezone.utc
                ).isoformat(),
                **audit_data,
            }
        else:
            entry = {
                "timestamp": datetime.fromtimestamp(
                    record.created, tz=timezone.utc
                ).isoformat(),
                "message": record.getMessage(),
            }
        return json.dumps(entry, default=str)


def get_audit_logger(
    log_path: str | None = None,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
) -> logging.Logger:
    """Return the shared ``hive_mind.audit`` logger with a RotatingFileHandler.

    If the logger already has a RotatingFileHandler pointing at *log_path*, a
    duplicate handler is **not** added.

    Args:
        log_path: Path to the audit log file. Defaults to ``<project>/audit.log``.
        max_bytes: Maximum size of a single log file before rotation (default 5 MB).
        backup_count: Number of rotated backup files to keep (default 3).
    """
    if log_path is None:
        log_path = _DEFAULT_LOG_PATH

    logger = logging.getLogger("hive_mind.audit")
    logger.setLevel(logging.INFO)

    # Avoid duplicate handlers when called more than once with the same path
    for handler in logger.handlers:
        if (
            isinstance(handler, RotatingFileHandler)
            and hasattr(handler, "baseFilename")
            and os.path.abspath(handler.baseFilename) == os.path.abspath(log_path)
        ):
            return logger

    handler = RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count
    )
    handler.setFormatter(JSONAuditFormatter())
    logger.addHandler(handler)
    return logger


def audit_wrap(func: Callable[..., Any], logger: logging.Logger) -> Callable[..., Any]:
    """Wrap *func* so that every call is audit-logged with timing and redacted args.

    The wrapper:
    - Captures all arguments (positional + keyword) as a dict
    - Redacts sensitive values before logging
    - Records wall-clock duration in milliseconds
    - Logs success or error status
    - Re-raises any exception unchanged

    Async functions receive an async wrapper so that
    ``inspect.iscoroutinefunction`` remains True and FastMCP can ``await`` them.
    """

    sig = inspect.signature(func)

    def _log_call(
        all_args: dict[str, Any],
        start: float,
        status: str,
        error_msg: str | None,
    ) -> None:
        duration_ms = round((time.monotonic() - start) * 1000, 2)
        audit_data = {
            "event": "tool_call",
            "tool": func.__name__,
            "args": redact_args(all_args),
            "status": status,
            "duration_ms": duration_ms,
            "error": error_msg,
        }
        logger.info(
            "tool_call: %s", func.__name__, extra={"audit_data": audit_data}
        )

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            all_args = dict(bound.arguments)

            start = time.monotonic()
            status = "success"
            error_msg: str | None = None

            try:
                result = await func(*args, **kwargs)
                return result
            except Exception as exc:
                status = "error"
                error_msg = str(exc)
                raise
            finally:
                _log_call(all_args, start, status, error_msg)

        return async_wrapper

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        all_args = dict(bound.arguments)

        start = time.monotonic()
        status = "success"
        error_msg: str | None = None

        try:
            result = func(*args, **kwargs)
            return result
        except Exception as exc:
            status = "error"
            error_msg = str(exc)
            raise
        finally:
            _log_call(all_args, start, status, error_msg)

    return wrapper
