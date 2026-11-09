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


def links(console_url: str, terminal_url: str) -> dict[str, str]:
    return {CONSOLE_URL: console_url, TERMINAL_URL: terminal_url}


def seed(env_ref) -> dict[str, str]:
    """The first-admin settings, as references into the generated .env."""
    return {SEED_USERNAME: env_ref("HIVE_SEED_USERNAME"),
            SEED_PASSWORD: env_ref("HIVE_SEED_PASSWORD")}
