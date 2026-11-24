"""Where this suite's scratch databases used to be implemented.

Moved into the package as `hive_data.testing` so the services moving onto the
one database share one sweep and one allow-list rather than each growing
their own. Kept as a re-export because every test file here imports from it,
and a rename across a suite is a diff nobody can read.
"""

from __future__ import annotations

from hive_data.testing import (  # noqa: F401
    ClaimedDatabase,
    PREFIXES,
    claimed_database,
    open_claimed,
    sweep_stale,
    with_database,
)
