"""Unified structured logging for the Hive control plane."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import time
import uuid
from collections import deque
from datetime import UTC, datetime
from threading import RLock
from typing import Any

_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("hive_log_context", default=None)
_SENSITIVE = re.compile(r"(^|_)(authorization|cookie|credential|password|private_key|secret|token)(_|$)", re.IGNORECASE)


def _safe(value: Any, key: str = "", depth: int = 0) -> Any:
    if _SENSITIVE.search(key):
        return "[REDACTED]"
    if depth >= 5:
        return "[MAX_DEPTH]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 4096 else value[:4096] + "...[TRUNCATED]"
    if isinstance(value, dict):
        return {str(k): _safe(v, str(k), depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(v, depth=depth + 1) for v in list(value)[:100]]
    return str(value)


# An in-process feed: bounded independently of how often the UI is opened.
_ERRORS = deque(maxlen=100)
# Re-entrant: a handler that logs an error while this thread holds the lock
# must not hang the process that is logging.
_ERROR_LOCK = RLock()
_STARTED_AT = datetime.now(UTC).isoformat()
_DIAGNOSTIC_LIMIT = 4096
# How much of a text is scrubbed, and how much of its end is dropped when it
# was longer than that: longer than anything the address pattern can match.
_SCRUB_WINDOW = 16 * _DIAGNOSTIC_LIMIT
_FRAGMENT = 2048


def _diagnostic_text(value: str) -> str:
    # This runs on the thread that logged - in a server, the event loop - so
    # its cost is bounded whatever the text: only a window is scrubbed, and
    # every pattern is linear in it. The address pattern used to start at the
    # scheme and was quadratic in an unbroken run: a 100,000-character error
    # held the loop for seconds.
    cut = len(value) > _SCRUB_WINDOW
    value = value[:_SCRUB_WINDOW]
    value = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)((?:token|password|secret|api[_-]?key)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", value)
    value = re.sub(r"(://[^/\s:@]{1,256}:)[^@\s]{1,1024}@", r"\1[REDACTED]@", value)
    longest = _FRAGMENT
    # A copy: another thread may set a variable while this one is logging.
    for key, secret in list(os.environ.items()):
        if (_SENSITIVE.search(key) or key.upper().endswith("_API_KEY")) and len(secret) >= 8:
            value = value.replace(secret, "[REDACTED]")
            longest = max(longest, len(secret))
    if cut:
        # The window may have ended inside a credential, which then matched
        # nothing above. Whatever part of one is left is at the very end.
        value = value[:max(0, len(value) - longest)]
    return value[:_DIAGNOSTIC_LIMIT]


def recent_errors() -> dict:
    with _ERROR_LOCK:
        retained = list(_ERRORS)
    cutoff = time.time() - 3600
    return {"started_at": _STARTED_AT,
            "errors": [dict(item) for stamp, item in reversed(retained) if stamp >= cutoff]}


class HiveJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "service": os.getenv("HIVE_SERVICE", record.name),
            "logger": record.name,
            "message": record.getMessage(),
            **_safe(_context.get() or {}),
            **_safe(getattr(record, "hive_fields", {})),
        }
        event = getattr(record, "hive_event", None)
        if event:
            data["event"] = event
        if record.exc_info:
            data["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else "Exception",
                "message": _safe(str(record.exc_info[1])),
                "stacktrace": self.formatException(record.exc_info),
            }
        if record.levelno >= logging.ERROR:
            item = {
                "timestamp": data["timestamp"], "level": record.levelname,
                "message": _diagnostic_text(record.getMessage()),
                "detail": _diagnostic_text(str(record.exc_info[1])) if record.exc_info else "",
            }
            with _ERROR_LOCK:
                _ERRORS.append((record.created, item))
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def configure_logging(service: str) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(os.getenv("HIVE_LOG_LEVEL", "INFO"))
    if not any(getattr(handler, "_hive_handler", False) for handler in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler._hive_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(HiveJsonFormatter())
        root.addHandler(handler)
    for name in ("httpx", "httpcore", "aiohttp.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return logging.getLogger(service)


def log_event(logger: logging.Logger, event: str, *, level: int = logging.INFO, exc_info=None, **fields: Any) -> None:
    try:
        logger.log(level, event, extra={"hive_event": event, "hive_fields": _safe(fields)}, exc_info=exc_info)
    except Exception:  # noqa: BLE001 - logging must be fail-open
        return


def install_fastapi_logging(app: Any, logger: logging.Logger, component: str) -> None:
    @app.middleware("http")
    async def request_logging(request, call_next):
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming[:128] if incoming.isprintable() else ""
        request_id = request_id or uuid.uuid4().hex
        token = _context.set({"request_id": request_id, "component": component})
        started = time.monotonic()
        important = request.method not in {"GET", "HEAD", "OPTIONS"}
        if request.url.path != "/health":
            log_event(logger, "http.request.started", level=logging.INFO if important else logging.DEBUG,
                      method=request.method, path=request.url.path)
        try:
            response = await call_next(request)
            level = logging.ERROR if response.status_code >= 500 else logging.WARNING if response.status_code >= 400 else logging.INFO if important else logging.DEBUG
            if request.url.path != "/health":
                log_event(logger, "http.request.completed", level=level, method=request.method,
                          path=request.url.path, status_code=response.status_code,
                          elapsed_ms=round((time.monotonic() - started) * 1000, 1))
            response.headers["x-request-id"] = request_id
            return response
        except Exception:
            log_event(logger, "http.request.failed", level=logging.ERROR, method=request.method,
                      path=request.url.path, elapsed_ms=round((time.monotonic() - started) * 1000, 1), exc_info=True)
            raise
        finally:
            _context.reset(token)
