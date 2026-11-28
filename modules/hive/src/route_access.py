"""`import route_access` — the shared module, bound here under its bare name.

The authorization middleware in front of every route and websocket. It was
byte-identical in the console and the terminal site, which is the same
standing hazard the account store just stopped being: one of the two gets a
fix and the other keeps the hole. Bound rather than re-exported, for the
reason `auth.py` beside this gives.
"""

import sys
from typing import TYPE_CHECKING

from hive_site import route_access as _module

sys.modules[__name__] = _module

if TYPE_CHECKING:
    # Only for the type checker, which cannot follow a module swapped into
    # `sys.modules` and would report every name this site reads through
    # `route_access.` as missing. Never executed: at runtime the line above has
    # already replaced this module with the real one.
    from hive_site.route_access import *  # noqa: F401,F403
