"""Unified, dependency-free logging for Hive Mind services.

The public API intentionally wraps the standard library instead of replacing it:
existing ``logger.info(...)`` calls keep working, while ``log_event`` adds the
stable event names and structured fields needed for incident research.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "hive_log_context", default=None
)
_SENSITIVE_KEY = re.compile(
    r"(^|_)(authorization|cookie|credential|password|private_key|secret|token)(_|$)",
    re.IGNORECASE,
)
_STANDARD_RECORD_KEYS = set(logging.makeLogRecord({}).__dict__)


def _safe(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Return a JSON-safe, bounded, recursively redacted value."""
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if depth >= 5:
        return "[MAX_DEPTH]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 4096 else value[:4096] + "...[TRUNCATED]"
    if isinstance(value, dict):
        return {
            str(k): _safe(v, key=str(k), depth=depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_safe(v, depth=depth + 1) for v in list(value)[:100]]
    return _safe(str(value), key=key, depth=depth + 1)


class HiveJsonFormatter(logging.Formatter):
    """One JSON object per record, suitable for container log collectors."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        service = getattr(record, "hive_service", None) or os.getenv(
            "HIVE_SERVICE", os.getenv("MIND_ID", record.name)
        )
        payload["service"] = service
        event_name = getattr(record, "hive_event", None)
        if event_name:
            payload["event"] = event_name
        payload.update(_safe(_context.get() or {}))
        payload.update(_safe(getattr(record, "hive_fields", {})))

        # Preserve explicitly supplied logging extras for callers that have not
        # migrated to log_event yet. Internal LogRecord attributes are ignored.
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_KEYS and not key.startswith("hive_"):
                payload.setdefault(key, _safe(value, key=key))

        if record.exc_info:
            payload["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else "Exception",
                "message": _safe(str(record.exc_info[1])),
                "stacktrace": self.formatException(record.exc_info),
            }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(service: str, *, level: str | int | None = None) -> logging.Logger:
    """Configure the process once and return a service logger.

    ``HIVE_LOG_FORMAT=text`` is available as an operator escape hatch. JSON is
    the default and all records still go to stderr so Docker/systemd capture
    semantics remain unchanged.
    """
    chosen_level = level if level is not None else os.getenv("HIVE_LOG_LEVEL", "INFO")
    root = logging.getLogger()
    root.setLevel(chosen_level)
    if not any(getattr(h, "_hive_handler", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler._hive_handler = True  # type: ignore[attr-defined]
        if os.getenv("HIVE_LOG_FORMAT", "json").lower() == "text":
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
            )
        else:
            handler.setFormatter(HiveJsonFormatter())
        root.addHandler(handler)

    # High-frequency client libraries should report failures, not polling.
    for noisy_logger in ("httpx", "httpcore", "aiohttp.access"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
    return logging.getLogger(service)


def log_event(
    logger: logging.Logger,
    event_name: str,
    *,
    level: int = logging.INFO,
    message: str | None = None,
    exc_info: bool | BaseException | tuple | None = None,
    **fields: Any,
) -> None:
    """Emit a searchable event without risking failures on unusual values."""
    try:
        logger.log(
            level,
            message or event_name,
            extra={"hive_event": event_name, "hive_fields": _safe(fields)},
            exc_info=exc_info,
        )
    except Exception:  # noqa: BLE001 - logging must be fail-open
        # Logging is diagnostic infrastructure. It must never change the
        # success/failure semantics of the operation being observed.
        return


def log_if_slow(
    logger: logging.Logger,
    event_name: str,
    elapsed_ms: float,
    *,
    threshold_ms: float = 30_000,
    **fields: Any,
) -> None:
    """Emit a warning when an operation exceeds its latency budget."""
    if elapsed_ms > threshold_ms:
        log_event(
            logger,
            event_name,
            level=logging.WARNING,
            elapsed_ms=elapsed_ms,
            threshold_ms=threshold_ms,
            **fields,
        )


def bind_context(**fields: Any) -> contextvars.Token:
    """Add correlation fields for the current async context."""
    merged = dict(_context.get() or {})
    merged.update(_safe(fields))
    return _context.set(merged)


def reset_context(token: contextvars.Token) -> None:
    _context.reset(token)


def install_fastapi_logging(
    app: Any,
    logger: logging.Logger,
    *,
    component: str,
    exclude_paths: set[str] | None = None,
) -> None:
    """Install correlated request, response, and uncaught-error events."""
    excluded = exclude_paths or {"/health"}

    @app.middleware("http")
    async def hive_request_logging(request: Any, call_next: Callable[[Any], Awaitable[Any]]):
        raw_request_id = request.headers.get("x-request-id", "")
        request_id = raw_request_id[:128] if raw_request_id.isprintable() else ""
        request_id = request_id or uuid.uuid4().hex
        token = bind_context(request_id=request_id, component=component)
        started = time.monotonic()
        important = request.method not in {"GET", "HEAD", "OPTIONS"}
        if request.url.path not in excluded:
            log_event(
                logger,
                "http.request.started",
                level=logging.INFO if important else logging.DEBUG,
                method=request.method,
                path=request.url.path,
            )
        try:
            response = await call_next(request)
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            level = logging.ERROR if response.status_code >= 500 else (
                logging.WARNING if response.status_code >= 400 else (
                    logging.INFO if important else logging.DEBUG
                )
            )
            if request.url.path not in excluded:
                log_event(
                    logger,
                    "http.request.completed",
                    level=level,
                    method=request.method,
                    path=request.url.path,
                    status_code=response.status_code,
                    elapsed_ms=elapsed_ms,
                )
            response.headers["x-request-id"] = request_id
            return response
        except Exception:
            log_event(
                logger,
                "http.request.failed",
                level=logging.ERROR,
                method=request.method,
                path=request.url.path,
                elapsed_ms=round((time.monotonic() - started) * 1000, 1),
                exc_info=True,
            )
            raise
        finally:
            reset_context(token)
