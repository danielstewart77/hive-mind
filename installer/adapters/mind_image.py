"""Deliverable G: the mind and voice images' build args and the time zone.

Assumed interface: the root Dockerfile and the voice Dockerfiles take build
args `UID` and `GID` (the container user's ids, so bind-mounted files are
the operator's); every service reads its time zone from `TZ`.
"""

from __future__ import annotations


def build_args(host) -> dict[str, str]:
    return {"UID": str(host.uid), "GID": str(host.gid)}


def tz(host) -> dict[str, str]:
    return {"TZ": host.tz}
