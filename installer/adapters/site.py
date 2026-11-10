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


# ---- pending: the console's env-file list (another builder) -------------
#: The console's credentials tab rewrites a fixed list of host .env files
#: (hive src/credentials_data.py) that this install does not mount. The list
#: is being made configurable; until its interface lands the installer mounts
#: nothing for it, and the tab shows nothing to rotate. Wire it here.
def env_files_settings(home) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """(env settings, (host path, container path) mounts) for the console's
    credentials tab. Empty until the interface lands."""
    del home
    return {}, []
