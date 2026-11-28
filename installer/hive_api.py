"""Writing holder credentials and the mind's registration into the hive database.

The installer holds no administrator credential of any service. What it has to
record — each service's own credential, the mind's registration, the mind's
one key in the tools — it writes as rows in the hive database through
`hive_data`, the way the gateway records a credential itself, and not by
asking a service that would have to trust an administrator value for it.

The write runs inside the hive network in the console image (which carries
`hive_data`), the same way the first admin is seeded; the database address and
the values ride in on stdin, never in a command line.

A holder that already has a row is never replaced: it is kept when the value
this install holds is the one the row recognises, and refused otherwise.
A holder is written with an explicit role — `service` for a service, `mind`
for the mind — because a role left out means `person`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from installer.shell import StepFailed

SERVICE = "service"
MIND = "mind"

#: Run by `Shell.in_network`, which defines `PAYLOAD` and imports json.
SCRIPT = r'''
import asyncio, hashlib, hmac, os, time
os.environ["HIVE_DATABASE_URL"] = PAYLOAD["dsn"]

LABEL = b"hive-mind: what the gateway shows this mind, v1"

def digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def gateway_credential(key):
    return hmac.new(str(key).strip().encode("utf-8"), LABEL, hashlib.sha256).hexdigest()

def held_by(holder):
    return (f"that value is already held by {holder!r}; "
            "a credential belongs to one holder")

async def mint(hive_data, p):
    from hive_data import broker_ops, core, graph
    done, error = {}, None
    comms = p["store"] == "comms"
    for holder, token in p["holders"].items():
        key = holder.strip().lower()
        sha = digest(token)
        existing = None
        if comms:
            existing = await core.get_credential(key)
        else:
            existing = next((r for r in await graph.credentials_all() if r["holder"] == key), None)
        if existing is not None:
            if existing["revoked_at"] is None and hmac.compare_digest(
                    str(existing["token_sha"]).encode(), sha.encode()):
                done[key] = "kept"
                continue
            error = (f"{p['store']} already has a holder named {key!r} with a credential this "
                     "install does not hold; it was not replaced. Choose another name or "
                     "revoke it on the console first.")
            break
        if comms:
            clash = await broker_ops.holder_of_digest(sha)
        else:
            clash = await graph.credential_holder_of_digest(token_sha=sha, other_than=key)
        if clash is not None and clash != key:
            error = held_by(clash)
            break
        now = time.time()
        try:
            if comms:
                async with hive_data.transaction() as unit:
                    await core.upsert_credential(holder=key, username=p["username"],
                                                 role=p["role"], token_sha=sha,
                                                 created_at=now, revoked_at=None, unit=unit)
                    if p["role"] == "mind":
                        known = await core.get_mind_by_id_or_name(key, unit=unit)
                        if known is not None:
                            await broker_ops.update_mind(
                                known["mind_id"], unit=unit,
                                session_token=gateway_credential(token))
            else:
                await graph.credential_put(holder=key, username=p["username"], role=p["role"],
                                           token_sha=sha, mind_id=p.get("mind_id"),
                                           created_at=now)
        except hive_data.Conflict:
            error = held_by(clash or "another holder")
            break
        done[key] = "minted"
    return {"done": done, "error": error}

async def register_mind(hive_data, p):
    from hive_data import broker_ops, core
    now = time.time()
    async with hive_data.transaction() as unit:
        existing = await core.get_mind(p["mind_id"], unit=unit)
        if existing:
            fields = {"name": p["name"], "gateway_url": p["gateway_url"], "model": p["model"],
                      "harness": p["harness"], "last_seen": now}
            if not existing["owner"]:
                fields["owner"] = p["owner"]
            await broker_ops.update_mind(p["mind_id"], unit=unit, **fields)
        else:
            await core.upsert_mind(mind_id=p["mind_id"], name=p["name"],
                                   gateway_url=p["gateway_url"], model=p["model"],
                                   harness=p["harness"], registered_at=now, last_seen=now,
                                   owner=p["owner"], unit=unit)
    return {"registered": True}

async def tools_key(hive_data, p):
    from hive_data import tools
    name = p["holder"].strip().lower()
    if name in ("hitl-approver", "internal", "skippy", "shared-token-retired"):
        return {"error": f"{name!r} is not a holder the tools take a mind's key for"}
    if len(p["token"]) < 8 or any(c.isspace() for c in p["token"]):
        return {"error": "a credential is at least 8 characters with no spaces in it"}
    try:
        await tools.register_token(name, digest(p["token"]), int(time.time()))
    except hive_data.Conflict as exc:
        return {"error": str(exc)}
    return {"registered": True}

async def main():
    import hive_data
    await hive_data.connect()
    try:
        op = PAYLOAD["op"]
        return await {"mint": mint, "mind": register_mind, "tools": tools_key}[op](
            hive_data, PAYLOAD["args"])
    finally:
        await hive_data.close()

try:
    print(json.dumps(asyncio.run(main())))
except Exception as exc:
    print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
'''


def apply(sh, dsn: str, op: str, image: str, **args) -> dict:
    """Run one operation of `SCRIPT` against the hive database and return its answer."""
    out = sh.in_network(SCRIPT, {"dsn": dsn, "op": op, "args": args}, timeout=120,
                        image=image, redact=(dsn, *(
                            str(v) for v in (args.get("holders") or {}).values()),
                            str(args.get("token") or "")))
    return out


def mint(sh, dsn: str, image: str, store: str, holders: dict[str, str], username: str,
         role: str = SERVICE, mind_id: str | None = None, on_minted=None) -> dict[str, str]:
    """Give every holder its credential unless it has one; never replace.

    `holders` maps holder -> the value this install holds for it. Returns
    holder -> "minted" | "kept". `on_minted(holder)` is called for every
    holder that was written, even when a later holder stops the run, so a run
    that fails at the next holder has already recorded this one as its own.
    """
    out = apply(sh, dsn, "mint", image, store=store, holders=holders, username=username,
                role=role, mind_id=mind_id)
    done = out.get("done") or {}
    if on_minted is not None:
        for holder, state in done.items():
            if state == "minted":
                on_minted(holder)
    if out.get("error"):
        raise StepFailed(f"{store}: {out['error']}")
    return done


def register_mind(sh, dsn: str, image: str, *, mind_id: str, name: str, gateway_url: str,
                  model: str, owner: str, harness: str = "dsh") -> None:
    out = apply(sh, dsn, "mind", image, mind_id=mind_id, name=name, gateway_url=gateway_url,
                model=model, harness=harness, owner=owner)
    if out.get("error") or not out.get("registered"):
        raise StepFailed(f"the mind {name!r} could not be registered: {out.get('error')}")


def register_tools_key(sh, dsn: str, image: str, holder: str, token: str) -> None:
    """The mind's one key as the holder's working credential in the tools."""
    out = apply(sh, dsn, "tools", image, holder=holder, token=token)
    if out.get("error") or not out.get("registered"):
        raise StepFailed(f"the tools could not take {holder!r}'s key: {out.get('error')}")


def answers(base_url: str, path: str = "/health", timeout: float = 15) -> int:
    """The status a service gives a request that carries no credential: it is
    serving when it answers at all (200, or 401/403 for a route that wants one)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base_url.rstrip("/") + path, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0
