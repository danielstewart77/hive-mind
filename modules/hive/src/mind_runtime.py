"""Reading and writing a Mind's runtime.yaml over its own HTTP API.

A Mind's `runtime.yaml` is the durable truth about what it is, and the Mind
re-registers from that file on every start — so the console has to write the
file, not just the broker row, or the next restart would undo the edit.

It writes it the same way for every Mind: `GET /runtime` and
`PATCH /runtime` against the gateway URL already in the broker registry.
That is one code path for a container in this stack, a bare-metal Mind on
this host, and a Mind on another machine — the last of which no bind mount
can reach. A Mind that is down cannot be configured, which is the honest
answer: the file is authoritative, and a broker row edited while its Mind
was unreachable would be overwritten the moment it came back.
"""

from __future__ import annotations

import contextvars
import urllib.parse

import httpx
from fastapi import HTTPException

_TIMEOUT = 8.0


#: The person signed in to the request being served, set around each
#: authorised request so the many places that reach a mind for them need not
#: each be handed the user. None outside a request, and on a bearer-only call.
_PERSON: contextvars.ContextVar[str | None] = contextvars.ContextVar("mind_runtime_person", default=None)
_PERSON_IS_ADMIN: contextvars.ContextVar[bool] = contextvars.ContextVar("mind_runtime_is_admin", default=False)


class PersonScope:
    """ASGI middleware (innermost): records who is signed in, for `send`.

    Added before the authorisation middleware so it runs inside it, where the
    request's user is already resolved and cached."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        person, admin = None, False
        try:
            import auth
            from starlette.requests import HTTPConnection

            user = await auth.resolve_user(HTTPConnection(scope))
            person = str((user or {}).get("username") or "").strip().lower() or None
            admin = bool((user or {}).get("is_admin")) if person else False
        except Exception:  # noqa: BLE001 - no person is not a failure of the request
            person, admin = None, False
        token = _PERSON.set(person)
        admin_token = _PERSON_IS_ADMIN.set(admin)
        try:
            await self.app(scope, receive, send)
        finally:
            _PERSON_IS_ADMIN.reset(admin_token)
            _PERSON.reset(token)


# ---------------------------------------------------------------------------
# Every change to a mind, and every reading of one, goes to the gateway
# ---------------------------------------------------------------------------
# Feature 834, requirements 23, 24 and 28 (story 900). The console never calls
# a mind's own address and never holds or sends a mind's key: it sends the
# request to the gateway, which works out whether this person may change that
# mind (its owner, an administrator) and delivers it with the credential the
# gateway holds for that mind. What the console sends is the signed-in
# person's own key and nothing else; with nobody signed in, or the person's
# key out of reach, nothing is sent (no fallback to a mind's key or to a
# credential of the console's own).
#
# `mind_url` throughout this module is the mind's own registered address plus
# the path wanted on it. It is only used to find which mind is meant and what
# is asked of it; it is never called.

#: Set by the gateway on every answer it relays from a mind, so an answer the
#: gateway made itself (a refusal, a missing route on an older gateway) is
#: never taken for the mind's.
DELIVERED_HEADER = "x-hive-forwarded"


def _gateway_base() -> str:
    import os

    return (os.getenv("GATEWAY_API_URL") or "http://hive-comms:8424").rstrip("/")


async def via_gateway(mind_url: str) -> tuple[str, dict[str, str]]:
    """Where to send a request meant for the mind at `mind_url`, and with what.

    Returns the gateway's forward URL for the same path and the signed-in
    person's own key. Refuses with a sentence, and never with 401 (the layout
    would sign the person out), when nobody is signed in, the mind is not in
    the registry, the person is neither the mind's owner nor an administrator,
    or the person's key cannot be had.
    """
    import configuration_data
    import holder_credentials

    person = _PERSON.get()
    if not person:
        raise HTTPException(
            status_code=403,
            detail="nobody is signed in, so nothing is sent to this mind",
        )
    parts = urllib.parse.urlsplit((mind_url or "").strip())
    row = await configuration_data._mind_at(f"{parts.scheme}://{parts.netloc}")
    mind_id = str((row or {}).get("id") or "").strip()
    if not mind_id:
        raise HTTPException(
            status_code=502,
            detail="this mind is not in the registry under an address of its own, "
            "so the gateway cannot be asked to reach it",
        )
    owner = str((row or {}).get("owner") or "").strip().lower()
    if owner and owner != person and not _PERSON_IS_ADMIN.get():
        raise HTTPException(
            status_code=403,
            detail="you are neither this mind's owner nor an administrator, so you cannot change it",
        )
    key = await holder_credentials.user_key(person)
    if not key:
        raise HTTPException(
            status_code=503,
            detail="your own key could not be found on this console, so nothing was sent to the mind",
        )
    tail = parts.path.lstrip("/") + (f"?{parts.query}" if parts.query else "")
    base = f"{_gateway_base()}/broker/minds/{urllib.parse.quote(mind_id, safe='')}/forward"
    return f"{base}/{tail}", {"Authorization": f"Bearer {key}"}


def _gateway_words(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("error") or "")[:300]
    return ""


def _gateway_refusal(response: httpx.Response) -> None:
    """Turn what the gateway itself said into a sentence the page can show.

    Only answers the gateway made itself land here; an answer relayed from the
    mind carries `DELIVERED_HEADER` and is the caller's to read. A 401 is never
    passed on as 401: the layout redirects every 401 to the sign-in page."""
    status = response.status_code
    if status < 400:
        return
    if DELIVERED_HEADER in response.headers:
        if status == 401:
            # The mind itself refused what the gateway presented for it.
            raise HTTPException(
                status_code=502,
                detail="the mind refused what the gateway presented for it; it refreshes when the mind next registers",
            )
        return
    words = _gateway_words(response)
    if status == 401:
        raise HTTPException(
            status_code=502,
            detail="the gateway did not accept your key; replace it under Account settings, Your key",
        )
    if status == 403:
        raise HTTPException(status_code=403, detail=words or "the gateway refused this change")
    if status == 404 and words in ("", "Not Found"):
        raise HTTPException(
            status_code=502,
            detail="the gateway does not take changes for minds yet; restart the gateway on the new release first",
        )
    if status == 504:
        raise HTTPException(
            status_code=504,
            detail=words or "the mind did not answer in time; it is not known whether the change was applied, reload to see",
        )
    raise HTTPException(status_code=status, detail=words or f"the gateway answered {status}")


async def send(client, method: str, mind_url: str, **kwargs) -> httpx.Response:
    """One request for the mind at `mind_url`, through the gateway, as the
    signed-in person. Raises `HTTPException` with a sentence for everything
    the gateway or the path to it got wrong; what comes back is the mind's own
    answer, for the caller to read."""
    url, headers = await via_gateway(mind_url)
    try:
        response = await client.request(method, url, headers=headers, **kwargs)
    except httpx.TimeoutException as exc:
        if isinstance(exc, httpx.ConnectTimeout):
            raise HTTPException(status_code=502, detail="the gateway could not be reached") from exc
        raise HTTPException(
            status_code=504,
            detail="no answer in time; it is not known whether it was applied, reload to see",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="the gateway could not be reached") from exc
    _gateway_refusal(response)
    return response


def _refused_key(response: httpx.Response) -> None:
    """Kept for callers that pass a relayed answer on: the mind refusing the
    gateway's credential reaches here as the gateway's own sentence, never as
    a 401 (which would sign the person out)."""
    if response.status_code == 401:
        raise HTTPException(
            status_code=502,
            detail="the mind refused what the gateway presented for it; it refreshes when the mind next registers",
        )


def _endpoint(mind_url: str) -> str:
    url = (mind_url or "").strip().rstrip("/")
    if not url:
        raise HTTPException(
            status_code=400, detail="This Mind has no gateway URL in the registry"
        )
    return f"{url}/runtime"


def _unwrap(response: httpx.Response) -> dict:
    if response.status_code == 404:
        raise HTTPException(
            status_code=501,
            detail="This Mind is running a version without the /runtime API",
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    body = response.json()
    configuration = body.get("configuration") if isinstance(body, dict) else None
    if not isinstance(configuration, dict):
        raise HTTPException(status_code=502, detail="Mind returned no configuration")
    return configuration


async def fetch_configuration(mind_url: str) -> dict:
    """This Mind's runtime configuration, straight from its own file.

    With the admin credential, like the write beside it. This read used to be
    open on the Mind, and asking without it once the Mind closed it answers
    401 — which the console's own layout turns into a redirect to the sign-in
    page, so the symptom would be the operator being logged out rather than
    anything about authorization.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await send(client, "GET", _endpoint(mind_url))
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Mind is unreachable: {exc}"
        ) from exc
    return _unwrap(response)


async def push_default_model(
    mind_url: str, default_model: str, provider: str = ""
) -> dict:
    """Write the provider and `default_model` into this Mind's runtime.yaml.

    Both travel in one PATCH so the Mind is never left holding a provider that
    does not host the model beside it.
    """
    payload: dict = {"default_model": default_model}
    if provider:
        payload["provider"] = provider
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await send(client, "PATCH", _endpoint(mind_url), json=payload)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Mind is unreachable: {exc}"
        ) from exc
    return _unwrap(response)


async def fetch_models(mind_url: str) -> list[dict]:
    """What this Mind may run, asked with this Mind's own credential.

    The Mind queries the inference proxy with its own client key, so the
    proxy's own per-client filtering decides the answer: a deployment this
    Mind would be refused never reaches the picker. A console that listed
    the union would offer settings the proxy rejects at the first turn,
    which surfaces as a Mind that stopped answering rather than as a choice
    that was never allowed.

    A Mind too old to serve the route, or one that cannot reach the proxy,
    yields an empty list rather than raising — the editor is still usable
    by typing a name, and an error here would hide the whole form.
    """
    url = (mind_url or "").strip().rstrip("/")
    if not url:
        return []
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await send(client, "GET", f"{url}/models")
    except (httpx.HTTPError, HTTPException):
        return []
    if response.status_code != 200:
        return []
    body = response.json()
    rows = body.get("models") if isinstance(body, dict) else None
    return [r for r in rows if isinstance(r, dict) and r.get("name")] if isinstance(rows, list) else []


# ---------------------------------------------------------------------------
# The surface token
# ---------------------------------------------------------------------------
# Set through the Mind, never from here. The token has to land where *that*
# Mind's surface reads it — its own `.env` on an edge install, a per-mind
# keyring key in the containerised stack — and this console can see neither:
# it holds no mount, and a Mind on another machine has none to offer. So the
# Mind writes, and what travels is one PUT.
#
# Nothing ever comes back. The Mind's read route reports whether it has a
# token the bot API accepts and which bot that is, and this relays exactly
# that — a console that could read tokens would put every bot in the hive one
# session cookie away from being impersonated.


def _surface_token_endpoint(mind_url: str, surface: str = "telegram") -> str:
    url = (mind_url or "").strip().rstrip("/")
    if not url:
        raise HTTPException(
            status_code=400, detail="This Mind has no gateway URL in the registry"
        )
    return f"{url}/surface-token?surface={surface}"


def _unwrap_surface_token(response: httpx.Response) -> dict:
    """What the Mind said, with its own refusal preserved.

    A 400 from the Mind means the *token* was refused, not the credential
    this console presented, and flattening the two would tell the operator to
    go and check a console setting that is working.
    """
    if response.status_code == 404:
        raise HTTPException(
            status_code=501,
            detail="This Mind is running a version without the surface-token API",
        )
    if response.status_code >= 400:
        detail = response.text
        try:
            body = response.json()
            if isinstance(body, dict) and body.get("error"):
                detail = str(body["error"])
        except ValueError:
            pass
        raise HTTPException(status_code=response.status_code, detail=detail)
    body = response.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="Mind returned no token state")
    return body


async def fetch_surface_token(mind_url: str, surface: str = "telegram") -> dict:
    """Whether this Mind's surface has a token the bot API accepts.

    Three states reach the page, because they have different remedies:
    nothing stored, stored and accepted, or stored and refused. A Mind that
    is unreachable, or too old to serve the route, reports as unknown rather
    than as a Mind with no token — the remedy for the first is to go and look
    at the machine, and for the second to paste a token that was never
    missing.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await send(client, "GET", _surface_token_endpoint(mind_url, surface))
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Mind is unreachable: {exc}"
        ) from exc
    state = _unwrap_surface_token(response)
    answered = str(state.get("surface") or "")
    if answered and answered != surface:
        raise HTTPException(
            status_code=502,
            detail=f"This Mind answered about its {answered} token, not {surface}",
        )
    if not answered and surface != "telegram":
        # A Mind that predates the parameter drops it without comment and
        # answers about Telegram. Reported as unknown rather than shown as a
        # working Discord bot that does not exist.
        raise HTTPException(
            status_code=501,
            detail="This Mind is running a version that knows only one surface",
        )
    return state


async def push_surface_token(
    mind_url: str, token: str, surface: str = "telegram"
) -> dict:
    """Hand a Mind a new bot token for it to verify and store.

    The Mind checks it against the bot API before storing, so a refusal
    leaves the working token in place. What comes back names the bot the
    token authenticates as, and never the token.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT + 12.0) as client:
            response = await send(
                client, "PUT", _surface_token_endpoint(mind_url, surface),
                json={"token": token},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Mind is unreachable: {exc}"
        ) from exc
    return _unwrap_surface_token(response)


# ---------------------------------------------------------------------------
# The GitHub token
# ---------------------------------------------------------------------------
# Set through the Mind for the same reason its bot token is: the value has to
# land where *that* Mind's own `git` and `gh` look — a per-mind keyring key in
# the containerised stack, its own `.env` on an edge host — and this console
# can see neither. So the Mind writes, and what travels is one PUT.
#
# Nothing comes back but a verdict and an account name. A console that could
# read these would hold every Mind's push credential behind one session
# cookie.


def _github_token_endpoint(mind_url: str) -> str:
    url = (mind_url or "").strip().rstrip("/")
    if not url:
        raise HTTPException(
            status_code=400, detail="This Mind has no gateway URL in the registry"
        )
    return f"{url}/github-token"


def _unwrap_github_token(response: httpx.Response) -> dict:
    """What the Mind said, with its own refusal preserved.

    A 400 from the Mind means the *token* was refused, not the credential this
    console presented; a 503 means the Mind cannot store one at all. Neither
    may become the other, and neither may become "unreachable" — the remedies
    are a new token, a compose change, and a trip to the machine.
    """
    if response.status_code == 404:
        raise HTTPException(
            status_code=501,
            detail="This Mind is running a version without the GitHub-token API",
        )
    if response.status_code >= 400:
        detail = response.text
        try:
            body = response.json()
            if isinstance(body, dict) and body.get("error"):
                detail = str(body["error"])
        except ValueError:
            pass
        raise HTTPException(status_code=response.status_code, detail=detail)
    body = response.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="Mind returned no token state")
    return body


async def push_github_token(mind_url: str, token: str) -> dict:
    """Hand a Mind a new GitHub token for it to verify and store.

    The Mind checks it against GitHub before storing, so a refusal leaves the
    working token in place. What comes back names the account the token
    authenticates as, and never the token.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT + 12.0) as client:
            response = await send(
                client, "PUT", _github_token_endpoint(mind_url), json={"token": token},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Mind is unreachable: {exc}"
        ) from exc
    return _unwrap_github_token(response)


# ---------------------------------------------------------------------------
# The voice
# ---------------------------------------------------------------------------
# A Mind's voice lives in that Mind's own runtime.yaml, written the way its
# model is, because the broker row the voice server reads is a cache of that
# file. The catalogue of what is selectable comes from the voice server
# instead: it is a property of the running speech engine and its language, not
# of any one Mind.


#: The speech engines a Mind may be spoken by. Chatterbox clones a reference
#: recording this Mind's directory holds; Kokoro speaks one of the server's
#: own catalogued voices.
CHATTERBOX = "chatterbox"
KOKORO = "kokoro"
VOICE_ENGINES: tuple[str, ...] = (CHATTERBOX, KOKORO)

#: What a Mind naming no engine is spoken by, matching the voice server's own
#: default — so a Mind that has never been through this panel is described the
#: way it actually sounds.
DEFAULT_VOICE_ENGINE = CHATTERBOX


async def push_voice(mind_url: str, voice: str, engine: str = "") -> dict:
    """Write this Mind's voice, and the engine that speaks it, into its file.

    On their own, without restating the model: a picker that resent the model
    it rendered a minute ago would write a stale one back over a fresh edit.

    Both in one request, because what a voice means depends on the engine — a
    Kokoro voice name under Kokoro, a clip filename under Chatterbox. Written
    separately, a Mind would briefly hold one engine's selection under the
    other, and whichever write failed would leave it there.
    """
    body: dict[str, str] = {"voice": voice}
    if engine:
        body["voice_engine"] = engine
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await send(client, "PATCH", _endpoint(mind_url), json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Mind is unreachable: {exc}"
        ) from exc
    return _unwrap(response)


#: Synthesising a sentence is slower than reading a config file, and the first
#: sample in a second accent waits for that pipeline to load.
_SAMPLE_TIMEOUT = 60.0


async def sample_voice(
    voice_server_url: str, voice: str, voice_id: str = "", headers: dict | None = None
) -> bytes:
    """A spoken sample of one voice, for the page to play.

    The grades the picker shows describe how a voice was trained, not what it
    sounds like, and nobody choosing the voice of a Mind they talk to daily
    should have to choose from a letter. A voice must be named: sampling
    "whatever the default is" answers a question nobody asked.

    Under Chatterbox the thing being judged is a recording rather than a
    catalogued voice, so the Mind is named too — the clip lives in that Mind's
    own directory, and the server has to be told whose.
    """
    name = (voice or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="A voice is required")
    url = (voice_server_url or "").strip().rstrip("/")
    if not url:
        raise HTTPException(
            status_code=502, detail="No voice server is configured to speak it"
        )
    body = {"voice": name}
    if voice_id:
        body["voice_id"] = voice_id
    try:
        async with httpx.AsyncClient(timeout=_SAMPLE_TIMEOUT) as client:
            response = await client.post(f"{url}/voices/sample", json=body, headers=headers or {})
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"The voice server is unreachable: {exc}"
        ) from exc
    if response.status_code != 200:
        # The server's own refusal, carried through rather than relabelled: it
        # knows which voices it offers and this process does not.
        raise HTTPException(
            status_code=response.status_code if response.status_code < 500 else 502,
            detail=f"The voice server would not speak {name}",
        )
    return response.content


async def fetch_voice_catalogue(voice_server_url: str) -> dict:
    """What the voice server can speak, for the picker to offer.

    A server that cannot be reached, or whose own catalogue fetch failed,
    comes back unreadable rather than empty — the page has to be able to say
    "could not read the list of voices", which is a different sentence from
    "this engine has no voices" and sends the operator somewhere else.
    """
    url = (voice_server_url or "").strip().rstrip("/")
    unreadable = {
        "engine": "",
        "default_voice": "",
        "catalogue": {
            "language_code": "",
            "readable": False,
            "detail": "The voice server could not be reached",
            "voices": [],
        },
    }
    if not url:
        return unreadable
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(f"{url}/voices")
    except httpx.HTTPError as exc:
        unreadable["catalogue"]["detail"] = f"The voice server could not be reached: {exc}"
        return unreadable
    if response.status_code != 200:
        unreadable["catalogue"]["detail"] = (
            f"The voice server answered {response.status_code}"
        )
        return unreadable
    body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("catalogue"), dict):
        unreadable["catalogue"]["detail"] = "The voice server returned no catalogue"
        return unreadable
    return body


# ---------------------------------------------------------------------------
# The settings panel
#
# One form in front of every Mind, whatever its harness. The shape of the
# form is not decided here: the Mind reports which controls its own runner
# honours, so a Claude Mind is not offered a goal-rounds box that nothing
# would read. This module relays and nothing more.
# ---------------------------------------------------------------------------


def _settings_endpoint(mind_url: str) -> str:
    return f"{_endpoint(mind_url)}/settings"


def _unwrap_settings(response: httpx.Response) -> dict:
    if response.status_code == 404:
        raise HTTPException(
            status_code=501,
            detail="This Mind is running a version without the settings API",
        )
    if response.status_code >= 400:
        detail = response.text
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("error"):
            detail = str(body["error"])
        raise HTTPException(status_code=response.status_code, detail=detail)
    body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("settings"), list):
        raise HTTPException(status_code=502, detail="Mind returned no settings")
    return body


async def fetch_settings(mind_url: str) -> dict:
    """The controls this Mind's harness honours, with their current values."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await send(client, "GET", _settings_endpoint(mind_url))
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Mind is unreachable: {exc}"
        ) from exc
    return _unwrap_settings(response)


async def push_settings(mind_url: str, settings: dict) -> dict:
    """Write the settings the operator changed into this Mind's runtime.yaml.

    Only the changed ones travel: a form that resent every field would write a
    value the operator never touched back over an edit made elsewhere since
    the page was opened.
    """
    if not settings:
        raise HTTPException(status_code=400, detail="Nothing was changed")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await send(
                client, "PATCH", _settings_endpoint(mind_url), json={"settings": settings},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Mind is unreachable: {exc}"
        ) from exc
    return _unwrap_settings(response)
