"""The one key a mind's chat bot presents: its mind's own, read from that
mind's key folder and from nowhere else.

A bot serves one mind and holds no credential of its own. The compose file
mounts that mind's key folder, read-only, into the bot and names the file in
`MIND_KEY_FILE`; the key is read at each request with a modification-time
cache, so a key the console replaces is the key the bot presents on its next
call, with no second step. A mind started before it had a key folder has its
key in `MIND_KEY`, which is the same key.

Standard library only: this package is also installed on machines that have no
other part of the hive.
"""

from __future__ import annotations

import os
import re

#: The shape of a key (the same the console accepts when one is pasted).
_SHAPE = re.compile(r"[A-Za-z0-9._~+/=-]{8,4096}")

_cache: dict[str, tuple[tuple[int, int] | None, str]] = {}


def current(environ=None) -> str:
    """This mind's key now, or "" when it has none.

    A file that cannot be read, or holds something that is not a key, keeps the
    last good key it held; before any good read, `MIND_KEY` stands.
    """
    env = os.environ if environ is None else environ
    path = str(env.get("MIND_KEY_FILE") or "").strip()
    if path:
        try:
            st = os.stat(path)
            stamp = (st.st_mtime_ns, st.st_size)
            held = _cache.get(path)
            if held is None or held[0] != stamp:
                with open(path, encoding="utf-8") as handle:
                    value = handle.read().strip()
                if _SHAPE.fullmatch(value):
                    _cache[path] = (stamp, value)
                else:
                    _cache[path] = (stamp, held[1] if held else "")
            if _cache[path][1]:
                return _cache[path][1]
        except OSError:
            held = _cache.get(path)
            if held and held[1]:
                return held[1]
    return str(env.get("MIND_KEY") or "").strip()


def bearer(environ=None) -> dict[str, str]:
    """The Authorization header for this mind, or none when it has no key."""
    key = current(environ)
    return {"Authorization": f"Bearer {key}"} if key else {}
