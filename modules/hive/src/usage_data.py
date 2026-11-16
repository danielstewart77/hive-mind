"""What each person's minds cost, from the inference proxy's meter.

Story 2.1.17, DECIDED 2026-11-15 by Daniel: one proxy key per mind, and usage
totalled per person across the minds they own - the mind's owner in the
hive. The proxy meters per holder and knows nothing of people; the hive
knows whose mind is whose. So the proxy answers per holder and this joins
the two. A person sees their own; an admin sees everyone's, and the hive's
own services, which belong to no person.
"""

from __future__ import annotations

from typing import Any

import holder_credentials
import route_access


class UsageUnavailable(Exception):
    """The proxy's meter could not be read; said, never shown as zero."""


async def proxy_usage(days: int) -> list[dict[str, Any]]:
    service = holder_credentials.SERVICES["proxy"]
    try:
        response = await holder_credentials._request(
            service, "GET", f"/credentials/usage?days={int(days)}")
    except holder_credentials.MintRefused as exc:
        raise UsageUnavailable(str(exc)) from exc
    if response.status_code != 200:
        raise UsageUnavailable(
            f"{service.label} answered the usage read with HTTP "
            f"{response.status_code}")
    rows = response.json()
    if not isinstance(rows, list):
        raise UsageUnavailable(f"{service.label} answered the usage read with no list")
    return [row for row in rows if isinstance(row, dict)]


def _totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "requests": sum(int(r.get("requests") or 0) for r in rows),
        "total_tokens": sum(int(r.get("total_tokens") or 0) for r in rows),
        "cost": round(sum(float(r.get("cost") or 0) for r in rows), 4),
    }


def usage_by_person(rows: list[dict[str, Any]], minds: list[dict],
                    user: dict | None) -> dict[str, Any]:
    """Each holder's usage under the person who owns that mind.

    A holder is a mind when a registered mind has its name; its person is
    that mind's owner. Anything else - a bot, the console, hive-tools - is
    the hive's own service and belongs to no one, so only an admin sees it.
    """
    owner_by_name = {
        str(mind.get("name") or "").strip().lower(): route_access.owner_of(mind)
        for mind in minds
    }
    people: dict[str, list[dict[str, Any]]] = {}
    services: list[dict[str, Any]] = []
    for row in rows:
        holder = str(row.get("holder") or "").strip().lower()
        owner = owner_by_name.get(holder)
        entry = {
            "holder": holder,
            "requests": int(row.get("requests") or 0),
            "total_tokens": int(row.get("total_tokens") or 0),
            "cost": round(float(row.get("cost") or 0), 4),
        }
        if owner:
            people.setdefault(owner, []).append(entry)
        else:
            services.append(entry)
    is_admin = bool(user and user.get("is_admin"))
    me = str((user or {}).get("username") or "").strip().lower()
    shown = sorted(people) if is_admin else [me] if me else []
    return {
        "people": [
            {"username": name, "minds": sorted(people.get(name, []),
                                                key=lambda r: r["holder"]),
             **_totals(people.get(name, []))}
            for name in shown
        ],
        "services": sorted(services, key=lambda r: r["holder"]) if is_admin else [],
    }
