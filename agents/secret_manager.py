"""Manage secrets via the system keyring.

Secrets are stored encrypted at rest in the Linux system keyring
(GNOME Keyring / KDE Wallet) using the 'hive-mind' service namespace.
Falls back to environment variables for secrets injected at launch.
"""

import json
import os

import keyring
from agent_tooling import tool

SERVICE_NAME = "hive-mind"
_REGISTRY_KEY = "_KEY_REGISTRY"

# Key naming allowlist: must end with _KEY, _SECRET, _TOKEN, _API,
# or start with HIVEMIND_
_ALLOWED_SUFFIXES = ("_KEY", "_SECRET", "_TOKEN", "_API")
_ALLOWED_PREFIX = "HIVEMIND_"


def _is_valid_key_name(key: str) -> bool:
    """Check if a key name matches the allowed naming patterns."""
    return key.startswith(_ALLOWED_PREFIX) or any(
        key.endswith(s) for s in _ALLOWED_SUFFIXES
    )


def _get_registry() -> list[str]:
    """Load the list of stored key names from the keyring."""
    raw = keyring.get_password(SERVICE_NAME, _REGISTRY_KEY)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []
    return []


def _save_registry(keys: list[str]) -> None:
    """Persist the list of stored key names to the keyring."""
    keyring.set_password(
        SERVICE_NAME, _REGISTRY_KEY, json.dumps(sorted(set(keys)))
    )


def get_credential(key: str) -> str | None:
    """Get a secret from keyring, falling back to environment variables.

    For use by other agents that need credentials (e.g. Neo4j tools).
    """
    return keyring.get_password(SERVICE_NAME, key) or os.getenv(key)


@tool(tags=["system"])
def set_secret(key: str, value: str) -> str:
    """Store a secret in the system keyring and load it into the current process.

    Args:
        key: Environment variable name (e.g. "STRIPE_API_KEY")
        value: The secret value

    Returns:
        Confirmation message.
    """
    key = key.strip().upper()
    if not key:
        return "Error: key cannot be empty."

    if not _is_valid_key_name(key):
        return (
            f"Error: key '{key}' is not allowed. "
            "Key names must end with _KEY, _SECRET, _TOKEN, or _API, "
            "or start with HIVEMIND_."
        )

    keyring.set_password(SERVICE_NAME, key, value)

    # Update registry
    registry = _get_registry()
    if key not in registry:
        registry.append(key)
        _save_registry(registry)

    # Make immediately available in current process
    os.environ[key] = value

    return f"Secret '{key}' stored in system keyring and loaded into environment."


@tool(tags=["system"])
def get_secret(key: str) -> str:
    """Check if a secret exists in the system keyring (does NOT reveal the value).

    Args:
        key: Environment variable name to check

    Returns:
        Whether the secret is configured.
    """
    key = key.strip().upper()

    # Check keyring first, then fall back to environment
    if keyring.get_password(SERVICE_NAME, key):
        return f"'{key}' is configured."

    if os.getenv(key):
        return f"'{key}' is configured (via environment)."

    return f"'{key}' is NOT configured."


@tool(tags=["system"])
def list_secrets() -> str:
    """List all keys stored in the system keyring (values are hidden).

    Returns:
        Newline-separated list of environment variable names from .env.
    """
    registry = _get_registry()
    if not registry:
        return "No secrets stored in the system keyring."
    return "Configured secrets:\n" + "\n".join(f"  - {k}" for k in sorted(registry))
