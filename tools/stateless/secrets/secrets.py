#!/usr/bin/env python3
"""Manage secrets via the system keyring.

Standalone stateless tool. Dependencies: keyring, keyrings.alt.

Secrets are stored in the system keyring using the 'hive-mind' service
namespace. Falls back to environment variables when keyring is unavailable.
"""

import argparse
import json
import logging
import os
import sys

# Allow importing core modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

log = logging.getLogger(__name__)

SERVICE_NAME = "hive-mind"
_REGISTRY_KEY = "_KEY_REGISTRY"

# Key naming allowlist: must end with an allowed suffix or start with HIVEMIND_
_ALLOWED_SUFFIXES = (
    "_KEY", "_SECRET", "_TOKEN", "_API",
    "_AUTH", "_URI", "_URL", "_EMAIL", "_PASSWORD", "_ID",
)
_ALLOWED_PREFIX = "HIVEMIND_"


def _keyring_get(key: str) -> str | None:
    """Safe wrapper around keyring.get_password -- returns None on failure."""
    try:
        import keyring
        return keyring.get_password(SERVICE_NAME, key)
    except Exception:
        return None


def _keyring_set(key: str, value: str) -> bool:
    """Safe wrapper around keyring.set_password -- returns success bool."""
    try:
        import keyring
        keyring.set_password(SERVICE_NAME, key, value)
        return True
    except Exception as exc:
        log.warning("keyring.set_password failed for '%s': %s", key, exc)
        return False


def _is_valid_key_name(key: str) -> bool:
    """Check if a key name matches the allowed naming patterns."""
    return key.startswith(_ALLOWED_PREFIX) or any(
        key.endswith(s) for s in _ALLOWED_SUFFIXES
    )


def _get_registry() -> list[str]:
    """Load the list of stored key names from the keyring."""
    raw = _keyring_get(_REGISTRY_KEY)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []
    return []


def _save_registry(keys: list[str]) -> None:
    """Persist the list of stored key names to the keyring."""
    _keyring_set(_REGISTRY_KEY, json.dumps(sorted(set(keys))))


def cmd_set(args: argparse.Namespace) -> int:
    """Store a secret in the system keyring."""
    key = args.key.strip().upper()
    if not key:
        print(json.dumps({"error": "Key cannot be empty."}))
        return 1

    if not _is_valid_key_name(key):
        print(json.dumps({
            "error": (
                f"Key '{key}' is not allowed. "
                f"Key names must end with one of {_ALLOWED_SUFFIXES}, "
                f"or start with {_ALLOWED_PREFIX}."
            )
        }))
        return 1

    if not _keyring_set(key, args.value):
        print(json.dumps({"error": f"Failed to store '{key}' -- no keyring backend available."}))
        return 1

    # Update registry
    registry = _get_registry()
    if key not in registry:
        registry.append(key)
        _save_registry(registry)

    # Make immediately available in current process
    os.environ[key] = args.value

    print(json.dumps({
        "stored": True,
        "key": key,
        "message": f"Secret '{key}' stored in system keyring and loaded into environment.",
    }))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    """Check if a secret exists (does NOT reveal the value)."""
    key = args.key.strip().upper()

    # Check keyring first, then fall back to environment
    if _keyring_get(key):
        print(json.dumps({"configured": True, "key": key, "source": "keyring"}))
        return 0

    if os.getenv(key):
        print(json.dumps({"configured": True, "key": key, "source": "environment"}))
        return 0

    print(json.dumps({"configured": False, "key": key}))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """List all keys stored in the system keyring (values are hidden)."""
    registry = _get_registry()
    print(json.dumps({"keys": sorted(registry), "count": len(registry)}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Hive Mind secret management tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sp_set = subparsers.add_parser("set", help="Store a secret")
    sp_set.add_argument("--key", required=True, help="Secret key name (e.g. STRIPE_API_KEY)")
    sp_set.add_argument("--value", required=True, help="Secret value")

    sp_get = subparsers.add_parser("get", help="Check if a secret exists")
    sp_get.add_argument("--key", required=True, help="Secret key name to check")

    subparsers.add_parser("list", help="List stored secret keys")

    args = parser.parse_args()

    commands = {
        "set": cmd_set,
        "get": cmd_get,
        "list": cmd_list,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
