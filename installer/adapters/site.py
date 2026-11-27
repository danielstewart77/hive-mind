"""Deliverable B: the env the console and the terminal take (hive_site).

Assumed interface:
- `HIVE_COOKIE_DOMAIN` unset means a host-only cookie; the session cookie is
  Secure only when the request is https (X-Forwarded-Proto honoured). The
  installer therefore sets no cookie domain at all.
- `HIVE_TERMINAL_URL` / `HIVE_CONSOLE_URL`: the other site's address for the
  nav links.
- `HIVE_SEED_USERNAME` / `HIVE_SEED_PASSWORD`: the first admin, seeded on an
  empty account store only.
"""

from __future__ import annotations

TERMINAL_URL = "HIVE_TERMINAL_URL"
#: The hive site's own address as a browser reaches it: the tools link an
#: approval notice to it, and the site names itself by it.
PUBLIC_URL = "HIVE_SITE_PUBLIC_URL"
CONSOLE_URL = "HIVE_CONSOLE_URL"
SEED_USERNAME = "HIVE_SEED_USERNAME"
SEED_PASSWORD = "HIVE_SEED_PASSWORD"  # secret-guard: allow — a variable name
#: Never set: unset is host-only, which is what an install by IP needs.
COOKIE_DOMAIN = "HIVE_COOKIE_DOMAIN"


#: The first admin, made with hive_site's own seed code (it writes only into
#: an empty account store) and then looked up. Run by `Shell.in_network` in
#: the console image, which defines PAYLOAD; the credentials arrive on stdin.
SEED_SCRIPT = r'''
import asyncio, os
os.environ["HIVE_DATABASE_URL"] = PAYLOAD["dsn"]
os.environ["HIVE_SEED_USERNAME"] = PAYLOAD["username"]
os.environ["HIVE_SEED_PASSWORD"] = PAYLOAD["password"]
async def main():
    from hive_site import auth, store
    await store.open()
    try:
        before = await store.get_user(auth.normalize_username(PAYLOAD["username"]))
        await auth.seed_first_user()
        after = await store.get_user(auth.normalize_username(PAYLOAD["username"]))
        return {"exists": after is not None, "created": before is None and after is not None}
    finally:
        await store.close()
try:
    print(json.dumps(asyncio.run(main())))
except Exception as exc:
    print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
'''


def links(console_url: str, terminal_url: str) -> dict[str, str]:
    return {CONSOLE_URL: console_url, TERMINAL_URL: terminal_url}


# ---- the console's credentials tab (hive PR #82) --------------------------
#: The credentials tab rewrites the .env files `HIVE_ENV_FILES` names, by
#: role, at the container paths given. The installer has one: its own .env,
#: bind-mounted read-write as a single file.
ENV_FILES = "HIVE_ENV_FILES"
ENV_FILE_MOUNT = "/env_hive/.env"
ENV_FILE_LABEL = "nervous_system"


def env_files_settings() -> dict[str, str]:
    import json
    return {ENV_FILES: json.dumps({ENV_FILE_LABEL: ENV_FILE_MOUNT})}


# ---- the console's Add a Mind page ----------------------------------------
#: Where the console writes a mind it creates: the install's minds folder,
#: writable. It reads every mind's runtime.yaml from the read-only mount of
#: the same folder beside it.
MINDS_WRITE_MOUNT = "/minds_hive_mind_config"
#: The list of the minds the console has created, as compose reads it: a file
#: in the minds folder that the stack file includes. The console adds a line
#: to it and takes one out; the installer makes it once, empty, and never
#: writes it again, so running the installer keeps every mind listed there.
MINDS_STACK = "stack.yaml"
MINDS_STACK_EMPTY = "include: []\n"


#: The file in the minds folder that names the release a new mind is built from.
RELEASE_CONTEXT_FILE = ".release-context"


def release_context(address: str, commit: str) -> str:
    """The build context of a mind the console creates: the repository's
    address at the exact commit the checkout is on, never a branch. Any
    credential in the address is dropped: the file sits where anyone on the
    host can read it, and the build fetches a public tree."""
    import re
    clean = re.sub(r"^(https?://)[^/]*@", r"\1", address)
    return f"{clean}#{commit}"


def new_mind_settings(*, proxy_url: str,
                      build_args: dict[str, str], reserved: list[str]) -> dict[str, str]:
    """What the console's Add a Mind page needs to work on an installed hive.

    Unset, each of these means Daniel's workstation: a separate env file for
    the minds, a stack file under a Dev folder, a proxy on the host. An
    installed hive has one `.env` and its own proxy.

    What a new mind is built from is not set here: it is the release the
    checkout is on, which `release_context` writes into the minds folder when
    the checkout is put on it, and the console reads from there.
    """
    import json
    settings = {
        # A new mind's three keys go into the one .env, under its own names.
        "HIVE_MIND_ENV_LABEL": ENV_FILE_LABEL,
        "HIVE_MIND_STACK_COMPOSE": f"{MINDS_WRITE_MOUNT}/{MINDS_STACK}",
        # Entries in that list are relative to it, and it sits among them.
        "HIVE_MIND_STACK_MINDS_PREFIX": ".",
        "HIVE_MIND_PROXY_URL": proxy_url,
        "HIVE_MIND_RESERVED_NAMES": ",".join(reserved),
    }
    if build_args:
        settings["HIVE_MIND_BUILD_ARGS"] = json.dumps(build_args, sort_keys=True)
    return settings
