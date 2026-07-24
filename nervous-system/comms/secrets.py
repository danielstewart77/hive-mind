"""Shared credential utility for hive-comms.

Copied from Skippy's core/secrets.py during Phase A1 scaffold. Inside
the comms container the keyring backend is typically absent, so this
falls through to environment variables — exactly what the .env file
mounted via env_file provides.
"""

import os

try:
    import keyring
except ImportError:
    keyring = None  # type: ignore[assignment]

SERVICE_NAME = "hive-mind"


def get_credential(key: str) -> str | None:
    """Get a secret from keyring, falling back to environment variables."""
    if keyring is not None:
        try:
            value = keyring.get_password(SERVICE_NAME, key)
            if value:
                return value
        except Exception:
            pass
    return os.getenv(key)
