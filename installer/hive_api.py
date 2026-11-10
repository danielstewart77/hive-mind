"""Talking to comms and lucent: holder credentials and the mind's registration.

Both stores' `PUT /credentials/{holder}` is mint-or-replace. Replacing is how
an install against an existing hive would cut a running service off without a
word, so the installer never puts over a holder that already has a row: a
holder it finds is kept when the value this install holds is the one the
store accepts, and refused otherwise. A holder is minted with an explicit role
— `service` for a service, `mind` for the mind — because an omitted role
defaults to `person`, which may not reach the routes the machinery drives.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from installer.shell import StepFailed

SERVICE = "service"
MIND = "mind"

#: A route each store answers to a machine holder and not to a stranger: what
#: "the token written to that service is accepted" is checked against.
ACCEPTS = {"comms": "/broker/minds", "lucent": "/graph/schema"}


class Api:
    def __init__(self, kind: str, base_url: str, admin_token: str, timeout: float = 15):
        self.kind = kind
        self.base = base_url.rstrip("/")
        self.admin = admin_token
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict | None = None,
                token: str | None = None) -> tuple[int, object]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Authorization": f"Bearer {token or self.admin}",
                     **({"Content-Type": "application/json"} if data else {})})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        except OSError as exc:
            raise StepFailed(f"{self.kind} at {self.base} cannot be reached: {exc}") from exc
        try:
            return status, json.loads(raw or b"null")
        except ValueError:
            return status, raw.decode("utf-8", "replace")

    def credentials(self) -> dict[str, dict]:
        status, rows = self.request("GET", "/credentials")
        if status != 200 or not isinstance(rows, list):
            raise StepFailed(f"{self.kind} would not list its credentials ({status})")
        return {r["holder"]: r for r in rows}

    def accepts(self, token: str) -> bool:
        status, _ = self.request("GET", ACCEPTS[self.kind], token=token)
        return status not in (401, 403)


def mint(api: Api, holders: dict[str, str], username: str, role: str = SERVICE,
         mind_id: str | None = None, on_minted=None) -> dict[str, str]:
    """Give every holder its credential unless it has one; never replace.

    `holders` maps holder -> the value this install holds for it. Returns
    holder -> "minted" | "kept". `on_minted(holder)` is called the moment a
    holder's PUT succeeds, so a run that fails at the next holder has
    already recorded this one as its own.
    """
    existing = api.credentials()
    out: dict[str, str] = {}
    for holder, token in holders.items():
        key = holder.strip().lower()
        if key in existing:
            if existing[key].get("live") and api.accepts(token):
                out[key] = "kept"
                continue
            raise StepFailed(
                f"{api.kind} already has a holder named {key!r} with a credential this "
                "install does not hold; it was not replaced. Choose another name or "
                "revoke it on the console first.")
        body: dict = {"username": username, "token": token, "role": role}
        if mind_id and api.kind == "lucent":
            body["mind_id"] = mind_id
        status, answer = api.request("PUT", f"/credentials/{key}", body)
        if status != 200:
            raise StepFailed(f"{api.kind} refused to mint {key!r}: {answer}")
        out[key] = "minted"
        if on_minted is not None:
            on_minted(key)
    return out


def register_mind(comms: Api, *, mind_id: str, name: str, gateway_url: str, model: str,
                  owner: str, harness: str = "dsh", session_token: str | None = None) -> dict:
    body = {"mind_id": mind_id, "name": name, "gateway_url": gateway_url, "model": model,
            "harness": harness, "owner": owner}
    if session_token:
        body["session_token"] = session_token
    status, answer = comms.request("POST", "/broker/minds", body)
    if status != 200:
        raise StepFailed(f"comms refused to register {name!r} ({status}): {answer}")
    return answer if isinstance(answer, dict) else {}
