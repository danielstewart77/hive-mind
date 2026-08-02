"""Shared fixtures for the nervous-system test suite.

`tests/conftest.py` covers the repo's top-level suite; this tree is its own
rootdir and inherits none of it, which is how these tests came to depend on
whatever the invoking shell exported.
"""

import os
import sys

# Ensure the project root is on sys.path so imports work
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# The harness modules resolve their mind folder from MIND_NAME at import
# time. Tests always run against the tracked minds/example config, never
# whatever mind the invoking shell happens to belong to — on an operator's
# own machine that is a real mind whose folder this repo does not ship.
os.environ["MIND_NAME"] = "example"

# Point every gateway variable at a closed port before anything imports the
# code under test. A mind now asks comms for a stored carry-forward when it
# opens a cold terminal, so a suite that inherits a live COMMS_URL issues
# real requests against the operator's running gateway and races them.
# Set rather than delete: anything calling `load_dotenv()` refills a deleted
# name from the real `.env`, but will not overwrite one that already has a
# value.
os.environ["COMMS_URL"] = "http://127.0.0.1:9"
os.environ["COMMS_ADMIN_BEARER_TOKEN"] = "test-token-never-valid"
os.environ["COMMS_BEARER_TOKEN"] = "test-token-never-valid"
