#!/usr/bin/env python3
"""How much of the hive's OpenAI and Anthropic subscription allowance is spent.

    provider_pressure.py

Reads the inference proxy's `GET /v1/providers/limits` — the route the Hive
console dashboard's allowance panel reads — and bands each window exactly as
the dashboard does. Read-only: one GET, nothing that could change a limit.

Prints one JSON object on stdout and exits 0. Exit 1 with
`{"error": ..., "kind": ...}` on stdout when the proxy cannot be reached,
refuses the key, or answers with something that is not a limits report;
exit 2, the same shape, for a malformed command line.

The proxy root is the first of `INFERENCE_PROXY_URL`, `DSH_PROXY_BASE_URL`,
`OPENAI_BASE_URL`, `ANTHROPIC_BASE_URL`; the key the first of
`HIVE_PROXY_KEY`, `MIND_PROXY_KEY`, `INFERENCE_PROXY_KEY`, `DSH_API_KEY`. A
DSH mind's shell has `HIVE_PROXY_KEY` and `OPENAI_BASE_URL`; its `DSH_*`
names are scrubbed before a command sees them.

The figures are read off traffic the proxy already carried, never polled, so
a provider nobody has used since the proxy started is `reporting: false` —
unknown, which is not the same claim as headroom.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional

#: The dashboard's thresholds (hive repo, `src/dashboard_data.py`
#: `WARN_PERCENT` / `HOT_PERCENT`). Kept equal to them so that what Stash
#: reads and what Daniel sees on the dashboard are the same claim.
WARN_PERCENT = 60.0
HOT_PERCENT = 85.0

URL_VARS = ("INFERENCE_PROXY_URL", "DSH_PROXY_BASE_URL", "OPENAI_BASE_URL",
            "ANTHROPIC_BASE_URL")
KEY_VARS = ("HIVE_PROXY_KEY", "MIND_PROXY_KEY", "INFERENCE_PROXY_KEY",
            "DSH_API_KEY")

ROUTE = "/v1/providers/limits"
#: The whole request, not each socket read: a proxy trickling bytes must not
#: hold the caller for longer than this.
TIMEOUT_SECONDS = 6.0
#: Far above any real report; a body larger than this is not one.
MAX_BODY_BYTES = 1 << 20

_RANK = {"ok": 0, "warn": 1, "hot": 2}


class Failed(Exception):
    """The reading could not be taken; `kind` names why."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def pressure_band(used_percent: float) -> str:
    """ok below 60, warn from 60 to 85 inclusive, hot above 85."""
    if used_percent > HOT_PERCENT:
        return "hot"
    if used_percent >= WARN_PERCENT:
        return "warn"
    return "ok"


def _number(value: Any) -> Optional[float]:
    """A finite number, or nothing — never a bool or a string."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _text(value: Any) -> Optional[str]:
    """A name or label as the proxy sent it, when it is a string at all."""
    return value if isinstance(value, str) else None


def is_local_provider(host: Any) -> bool:
    """A provider on this machine or network, which has no allowance to meter.

    The dashboard's rule: keyed on the host, not the provider's name.
    """
    if not host:
        return False
    bare = str(host).strip().lower().split("://")[-1].split("/")[0]
    bare = bare.rsplit(":", 1)[0].strip("[]")
    if bare in {"localhost", "::1"} or bare.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(bare)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def _reset_passed(resets_at: Optional[float], minutes: Optional[float],
                  age: Optional[float], now: float) -> Optional[bool]:
    """Whether the window this figure was read in has started again.

    With no reset time, a reading older than its whole window has certainly
    reset; anything younger nobody knows, and it says so.
    """
    if resets_at is not None:
        return resets_at <= now
    if minutes is not None and age is not None and age > minutes * 60:
        return True
    return None


def _window(window: dict, age: Optional[float], now: float) -> Optional[dict]:
    used = _number(window.get("used_percent"))
    if used is None:
        return None
    resets_at = _number(window.get("resets_at"))
    minutes = _number(window.get("window_minutes"))
    return {
        "label": _text(window.get("label")),
        "used_percent": used,
        "window_minutes": minutes,
        "resets_at": resets_at,
        "band": pressure_band(used),
        # A window that has started again from nothing; its figure
        # describes a window that no longer exists.
        "reset_passed": _reset_passed(resets_at, minutes, age, now),
    }


def summarise(payload: Any, now: float) -> dict:
    """The proxy's report reduced to what an assigner needs to read.

    Each hosted provider gets a band: the worst among its windows that have
    not reset, or "unknown" when it is not reporting or every window has
    reset. A stale reading whose window has not reset still counts — spend
    only rises until the reset, so the old figure is a floor. A window with
    no reset time counts too unless the reading is older than the window.

    `worst` is the worst band any provider is known to be in, and `unknown`
    names every provider with no current reading — which is not headroom.
    """
    rows = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise Failed("the proxy answered without a providers list", "BadReply")
    providers = []
    approaching = []
    for row in rows:
        if not isinstance(row, dict) or is_local_provider(row.get("host")):
            continue
        reporting = bool(row.get("reporting"))
        stale, age = bool(row.get("stale")), _number(row.get("age_seconds"))
        raw = row.get("windows") if reporting else []
        windows = [w for w in (_window(r, age, now) for r in
                               (raw if isinstance(raw, list) else [])
                               if isinstance(r, dict)) if w is not None]
        live = [w for w in windows if w["reset_passed"] is not True]
        band = (max((w["band"] for w in live), key=_RANK.__getitem__)
                if live else "unknown")
        name = _text(row.get("name"))
        providers.append({
            "name": name,
            "host": _text(row.get("host")),
            "reporting": reporting,
            "stale": stale,
            "age_seconds": age,
            "band": band,
            "windows": windows,
        })
        approaching.extend(
            {"provider": name, "window": w["label"],
             "used_percent": w["used_percent"], "band": w["band"],
             "resets_at": w["resets_at"], "stale": stale, "age_seconds": age}
            for w in live if w["band"] != "ok")
    known = [p["band"] for p in providers if p["band"] != "unknown"]
    return {
        "worst": max(known, key=_RANK.__getitem__) if known else "unknown",
        "unknown": [p["name"] for p in providers if p["band"] == "unknown"],
        "approaching": approaching,
        "providers": providers,
        "thresholds": {"warn": WARN_PERCENT, "hot": HOT_PERCENT},
        "observed_at": now,
    }


def _first(names: tuple[str, ...], env: dict) -> str:
    for name in names:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def proxy_root(env: dict) -> str:
    root = _first(URL_VARS, env).rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


def fetch(env: dict) -> Any:
    """The proxy's limits report, decoded. One GET; raises `Failed`."""
    root, key = proxy_root(env), _first(KEY_VARS, env)
    if not root:
        raise Failed(f"no proxy address: set one of {', '.join(URL_VARS)}",
                     "NotConfigured")
    if not key:
        raise Failed(f"no proxy key: set one of {', '.join(KEY_VARS)}",
                     "NotConfigured")
    try:
        request = urllib.request.Request(
            root + ROUTE, method="GET",
            headers={"Authorization": f"Bearer {key}"})
    except ValueError as exc:
        raise Failed(f"the proxy address {root!r} is not a URL: {exc}",
                     "NotConfigured") from None
    deadline = time.monotonic() + TIMEOUT_SECONDS
    try:
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as reply:
            body = b""
            while chunk := reply.read1(65536):
                body += chunk
                if time.monotonic() > deadline:
                    raise TimeoutError(f"no full reply within {TIMEOUT_SECONDS}s")
                if len(body) > MAX_BODY_BYTES:
                    raise Failed("the proxy's reply is too large to be a "
                                 "limits report", "BadReply")
    except urllib.error.HTTPError as exc:
        raise Failed(f"the proxy answered {exc.code} on {ROUTE}",
                     "Unauthorized" if exc.code == 401 else "HTTPError") from None
    except (ValueError, UnicodeError) as exc:
        # A key or address the request cannot carry: a newline or non-latin-1
        # character in the key, a non-numeric port, a bad hostname.
        raise Failed(f"the proxy address or key cannot be sent: {exc}",
                     "NotConfigured") from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise Failed(f"the proxy at {root} could not be reached: {exc}",
                     "Unreachable") from None
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise Failed("the proxy answered with something that is not JSON",
                     "BadReply") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is answered as the error it is, never followed: following
    it would hand the proxy key to whatever the redirect names."""

    def redirect_request(self, *args, **kwargs):  # type: ignore[override]
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class _Parser(argparse.ArgumentParser):
    """A usage error is a JSON error too, not argparse's prose on stderr."""

    def error(self, message: str) -> None:  # type: ignore[override]
        print(json.dumps({"error": f"{self.prog}: {message}",
                          "kind": "UsageError"}))
        raise SystemExit(2)


def main(argv: Optional[list[str]] = None) -> int:
    # No --help: its prose on stdout would break the one-JSON-object promise.
    _Parser(prog="provider_pressure.py", add_help=False,
            description=__doc__.splitlines()[0]).parse_args(argv)
    try:
        result = summarise(fetch(dict(os.environ)), time.time())
    except Failed as exc:
        print(json.dumps({"error": str(exc), "kind": exc.kind}))
        return 1
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
