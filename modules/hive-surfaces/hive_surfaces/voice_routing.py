"""Which voice server speaks for a mind.

Two speech engines exist. Chatterbox clones a reference recording; Kokoro
speaks one of its own catalogued voices. They are separate processes with
separate models, so "which engine" is also "which URL" — and that used to be
decided by whichever `VOICE_SERVER_URL` each caller happened to hold in its
own environment.

That put a per-mind setting in every caller's environment: changing one
mind's engine meant editing compose, a systemd unit and a scheduled task on
three machines, which is why no mind's engine was ever changed after install.
It is the same mistake `mind_voices` corrected for the voice *name*, one
level up.

So the engine travels the path the voice already travels: the mind's
`runtime.yaml` is the truth, the broker row is the cache, and every caller
asks the record rather than its own environment. A host that has only ever
configured one voice server keeps working unchanged — that single URL answers
for either engine, because a host with one server installed is a host where
every mind is spoken by it.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

CHATTERBOX = "chatterbox"
KOKORO = "kokoro"

#: Every engine there is.
ENGINES: tuple[str, ...] = (CHATTERBOX, KOKORO)

#: What a mind naming no engine is spoken by — what the voice server itself
#: has always defaulted to, so the field changes no mind's voice by arriving.
DEFAULT_ENGINE = CHATTERBOX

#: How long one listing is reused. Short, because an operator who has just
#: changed an engine on the console is about to test it; long enough that a
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


def engine_index(rows: list[dict]) -> dict[str, str]:
    """`{name or uuid -> engine}` for every mind that has named one.

    Both keys, because a caller may address a mind either way: the surfaces
    send a UUID and a person typing into a tool sends a short name. A row
    naming no engine contributes nothing rather than an empty string, so a
    mind that has chosen none falls through to the default instead of
    resolving to no server at all. An engine this build has never heard of is
    dropped for the same reason — a URL cannot be guessed from a name.
    """
    index: dict[str, str] = {}
    for row in rows:
        engine = str(row.get("voice_engine") or "").strip().lower()
        if engine not in ENGINES:
            continue
        for key in (row.get("name"), row.get("id"), row.get("mind_id")):
            key = str(key or "").strip()
            if key:
                index[key] = engine
    return index


class VoiceServerResolver:
    """Resolves a voice_id to the URL of the server that speaks for it.

    Never raises. A gateway that cannot be reached costs one sentence the
    engine its mind picked, which is a wrong-sounding reply; raising would
    cost every caller its reply entirely.
    """

    def __init__(
        self,
        comms_url: str,
        token: str,
        urls: dict[str, str],
        *,
        fallback_url: str = "",
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        timeout: float = 3.0,
        fetch=_fetch_minds,
        clock=time.monotonic,
    ) -> None:
        self._url = comms_url or ""
        self._token = token or ""
        self._urls = {
            engine: (urls or {}).get(engine, "").strip().rstrip("/")
            for engine in ENGINES
        }
        # What a host that configured one server and never named an engine
        # has. Not a third engine: the one URL answers for whichever engine
        # the mind picked, because that server is the only one installed.
        self._fallback = (fallback_url or "").strip().rstrip("/")
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
            log.warning("mind engine listing unreadable: %s", exc)
            # Keep whatever was last known and try again after the TTL rather
            # than hammering a gateway that is down once per spoken sentence.
            self._fetched_at = now
            return self._index
        self._index = engine_index(rows)
        self._fetched_at = now
        return self._index

    def engine_named(self, voice_id: str) -> str:
        """The engine this mind's record names, or empty if it names none.

        Empty is not the same answer as the default. A mind that has declared
        nothing is a mind nobody has moved, and the caller's own configuration
        is still the best evidence of which server speaks it.
        """
        key = str(voice_id or "").strip()
        if not key:
            return ""
        return self._index_now().get(key) or ""

    def engine(self, voice_id: str) -> str:
        """Which engine speaks this mind, with the default applied."""
        return self.engine_named(voice_id) or DEFAULT_ENGINE

    def resolve(self, voice_id: str) -> str:
        """The voice server URL to call for this mind. Empty if none is set.

        A mind that names an engine is spoken by that engine's server. A mind
        that names none keeps the server this caller was already pointed at,
        which is what makes deploying this field change nobody's voice: two
        minds on this hive were deliberately pointed at the Kokoro server by
        their own `VOICE_SERVER_URL` and have never declared an engine, and
        resolving them to the default would move one onto a cloned voice and
        silence the other.
        """
        named = self.engine_named(voice_id)
        if named:
            return self.url_for(named)
        return self._fallback or self.url_for(DEFAULT_ENGINE)

    def url_for(self, engine: str) -> str:
        """The URL configured for one engine, or the single-server fallback."""
        return self._urls.get(engine) or self._fallback
