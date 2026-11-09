"""What an install may not do to a database that already holds a hive.

A hive is present when the database has an account. Then the admin the
operator names must be one of those accounts — the console seeds an admin
only on an empty account store, so a new name would be accepted and then
never exist — and a mind or service name already in use is refused, because
both stores' credential routes replace a holder outright and a mind
registration under a taken name rewrites that mind's row.

Names are compared folded: the console treats `my-mind` and `my_mind` as one
holder.
"""

from __future__ import annotations

import re


def fold(name: object) -> str:
    return re.sub(r"[-_\s]+", "-", str(name or "").strip().lower())


def hive_present(facts: dict) -> bool:
    return bool(facts.get("users"))


def refusals(facts: dict, *, admin: str, mind_name: str, mind_id: str,
             holders: dict[str, list[str]], ours: dict[str, list[str]]) -> list[str]:
    """Every reason this install may not go ahead against this database.

    `holders` is what this install would mint, per store; `ours` what an
    earlier run of this same install minted, which are its own to keep.
    """
    if not hive_present(facts):
        return []
    out: list[str] = []
    users = {fold(u) for u in facts.get("users") or []}
    if fold(admin) not in users:
        out.append(f"the admin {admin!r} has no account on this hive; name an existing "
                   "account as the admin")
    for row in facts.get("minds") or []:
        if fold(row.get("name")) == fold(mind_name) and row.get("mind_id") != mind_id:
            out.append(f"a mind named {row.get('name')!r} is already registered on this hive; "
                       "choose another name for the mind")
    for store, key in (("comms", "comms_holders"), ("lucent", "lucent_holders")):
        taken = {fold(h) for h in facts.get(key) or []}
        own = {fold(h) for h in (ours or {}).get(store, [])}
        for holder in holders.get(store, []):
            if fold(holder) in taken and fold(holder) not in own:
                out.append(f"{store} already has a holder named {holder!r}; "
                           "this install would replace its credential")
    return out
