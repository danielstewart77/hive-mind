"""Shared credential utility for Hive Mind.

Provides get_credential() for use by both stateful tools (via Python import)
and stateless tools (via sys.path manipulation). Falls back to environment
variables when keyring is unavailable.
"""

import os

# KEY_RING is our convention for the keyring storage directory.
# Must be applied before keyring is imported so PlaintextKeyring picks it up.
_key_ring_dir = os.getenv("KEY_RING")
if _key_ring_dir:
    os.environ["XDG_DATA_HOME"] = _key_ring_dir

try:
    import keyring
except ImportError:
    keyring = None  # type: ignore[assignment]

SERVICE_NAME = "hive-mind"


def get_credential(key: str) -> str | None:
    """Get a secret from keyring, falling back to environment variables.

    Args:
        key: The credential key name (e.g. "COINGECKO_API_KEY").

    Returns:
        The credential value, or None if not found in either source.
    """
    if keyring is not None:
        try:
            value = keyring.get_password(SERVICE_NAME, key)
            if value:
                return value
        except Exception:
            pass
    return os.getenv(key)
