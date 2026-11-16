"""Shared test fixtures for Hive Mind test suite."""

import sys
import os


# Ensure the project root is on sys.path so imports work
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# The harness modules resolve their mind folder from MIND_NAME at import
# time. Tests always run against the tracked minds/example config, never
# whatever mind the invoking shell happens to belong to.
os.environ["MIND_NAME"] = "example"

# A mind guards its session routes with a credential it mints beside its
# runtime.yaml. Naming one here keeps the suite from writing a real secret
# into the tracked minds/example directory, and gives the tests that drive
# those routes a value to present.
os.environ["MIND_SESSION_TOKEN"] = "test-mind-session-token"


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _gateway_says_unmanaged(monkeypatch):
    """The gateway's answer to "what may this mind use", for every test.

    A dsh spawn asks the gateway for the mind's role grants (feature 10.18)
    and refuses to start without an answer. No test here runs a gateway, so
    the network call — and only that — answers as a gateway would for a
    mind nobody has put under role management. A test about roles replaces
    it with the answer it needs.
    """
    from minds.harness import role_guard

    monkeypatch.setattr(role_guard, "fetch", lambda *a, **k: {"managed": False})
