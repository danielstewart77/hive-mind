"""Which voice a mind speaks in, resolved from the mind's own record.

A mind's voice used to live in `KOKORO_VOICE_MAP`, a JSON blob in this
container's environment read once at startup. That put a per-mind setting in a
shared service's environment: changing one mind's voice meant editing compose
and restarting a container that reloads a speech model, which is why no mind's
voice was ever changed after install.

`runtime.yaml` is the durable truth about a mind and the broker row is a cache
of it, exactly as for the model. So the voice travels the path the model
already travels — the mind registers it on every boot, and this server reads
the cache. A change takes effect on the next spoken sentence.

The environment map is still honoured underneath, for a mind running code that
predates the field. The record wins when both have an answer: the map is a
fallback, not a competing source of truth.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

#: How long one listing is reused. Short, because an operator who has just
#: changed a voice on the console is about to test it; long enough that a
#: sentence of speech does not cost an HTTP round trip per call.
DEFAULT_TTL_SECONDS = 30.0


def _fetch_minds(url: str, token: str, timeout: float) -> list[dict]:
    """The broker's mind listing. The service token is enough; no admin."""
    request = urllib.request.Request(
        url.rstrip("/") + "/broker/minds",
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    if isinstance(body, dict):
        body = body.get("minds") or []
    return [row for row in body if isinstance(row, dict)]


def voice_index(rows: list[dict]) -> dict[str, str]:
    """`{name or uuid -> voice}` for every mind that has named one.

    Both keys, because a caller may address a mind either way: the surfaces
    send a UUID and a person typing into a tool sends a short name. A row with
    no voice contributes nothing rather than an empty string, so a mind that
    has chosen none falls through to the default instead of being resolved to
    the empty voice.
    """
    index: dict[str, str] = {}
    for row in rows:
        voice = str(row.get("voice") or "").strip()
        if not voice:
            continue
        for key in (row.get("name"), row.get("id"), row.get("mind_id")):
            key = str(key or "").strip()
            if key:
                index[key] = voice
    return index


class MindVoiceResolver:
    """Resolves a voice_id to a voice name, record first.

    Never raises. A gateway that cannot be reached costs one sentence the voice
    its mind picked, which is a wrong-sounding reply; raising would cost every
    caller its reply entirely.
    """

    def __init__(
        self,
        comms_url: str,
        token: str,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        timeout: float = 3.0,
        fetch=_fetch_minds,
        clock=time.monotonic,
    ) -> None:
        self._url = comms_url or ""
        self._token = token or ""
        self._ttl = ttl_seconds
        self._timeout = timeout
        self._fetch = fetch
        self._clock = clock
        self._index: dict[str, str] = {}
        self._fetched_at: float | None = None

    def _index_now(self) -> dict[str, str]:
        if not self._url:
            return {}
        now = self._clock()
        if self._fetched_at is not None and now - self._fetched_at < self._ttl:
            return self._index
        try:
            rows = self._fetch(self._url, self._token, self._timeout)
        except Exception as exc:
            log.warning("mind voice listing unreadable: %s", exc)
            # Keep whatever was last known and try again after the TTL rather
            # than hammering a gateway that is down once per spoken sentence.
            self._fetched_at = now
            return self._index
        self._index = voice_index(rows)
        self._fetched_at = now
        return self._index

    def resolve(
        self, voice_id: str, *, env_map: dict[str, str], default: str
    ) -> str:
        """The voice `voice_id` should be spoken in."""
        key = str(voice_id or "").strip()
        if key:
            from_record = self._index_now().get(key)
            if from_record:
                return from_record
            from_env = (env_map or {}).get(key)
            if from_env:
                return from_env
        return default
