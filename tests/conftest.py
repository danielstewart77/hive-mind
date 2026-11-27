"""Shared test fixtures for Hive Mind test suite."""

import sys
import os


# Ensure the project root is on sys.path so imports work
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# The harness modules resolve their mind folder from MIND_NAME at import
# time. Tests always run against the tracked minds/example config, never
# whatever mind the invoking shell happens to belong to.
os.environ["MIND_NAME"] = "example"

# What the gateway shows a mind is made from the mind's one key, so no second
# secret is named here and nothing is written into the tracked minds/example.

# The harness modules also resolve their config homes from the environment at
# import time, and a mind's startup writes its rotation hooks and role guard
# files into them. Run from inside a mind's own session, the suite would
# otherwise write those into that mind's live Claude and Codex settings,
# pointing at this checkout. The homes are throwaway directories instead, for
# this process and every worker it spawns.
import tempfile  # noqa: E402

import atexit  # noqa: E402
import shutil  # noqa: E402

_SUITE_HOMES = tempfile.mkdtemp(prefix="hive-mind-suite-homes-")
atexit.register(shutil.rmtree, _SUITE_HOMES, ignore_errors=True)
for _variable, _folder in (("CLAUDE_CONFIG_DIR", "claude"), ("CODEX_HOME", "codex")):
    os.makedirs(os.path.join(_SUITE_HOMES, _folder))
    os.environ[_variable] = os.path.join(_SUITE_HOMES, _folder)
# Set rather than deleted, so a `load_dotenv()` cannot refill them: a dsh home
# nobody made is an undeclared one, and an empty hook file name is none.
os.environ["DSH_HOME"] = os.path.join(_SUITE_HOMES, "dsh-never-made")
os.environ["DSH_HOOKS_CONFIG"] = ""


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
