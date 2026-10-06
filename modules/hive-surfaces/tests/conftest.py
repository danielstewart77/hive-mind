"""Shared fixtures for the hive-surfaces suite."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Point every gateway variable at a closed port before anything imports the
# code under test. Set, never delete: a host's own `.env` loading later would
# refill `COMMS_URL` and the suite would start issuing live requests at a
# running hive-comms. A value already present is one nothing else overwrites.
os.environ["COMMS_URL"] = "http://127.0.0.1:9"
os.environ["COMMS_ADMIN_BEARER_TOKEN"] = "test-token-never-valid"
os.environ["COMMS_BEARER_TOKEN"] = "test-token-never-valid"
os.environ["MIND_SESSION_TOKEN"] = "test-mind-session-token"


import pytest


@pytest.fixture(autouse=True)
def _no_leaked_http_session():
    """Restore the surface's module-level session around every test.

    `_on_startup` assigns `telegram_bot.http` as a global, so a test that
    drives startup with `aiohttp` patched leaves a `MagicMock` behind that
    `patch.object` never restores — it restored `aiohttp`, not the global
    startup wrote. The next test to reach `_on_shutdown` then awaits that
    mock and fails for a reason that has nothing to do with what it guards,
    and only in whichever file order puts it second.
    """
    import hive_surfaces.telegram_bot as tb

    before = tb.http
    try:
        yield
    finally:
        tb.http = before
