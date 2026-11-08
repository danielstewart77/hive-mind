"""`import store` — the shared data store, bound here under its bare name.

The terminal performs no board, design or memory operation; what it needs of
this module is the twenty-two account operations under `auth`, and the
lifespan's `open` and `close`. It arrives whole because a unit of work has to
be one connection: see `hive_site/README.md`.
"""

import sys

from hive_site import store as _module

sys.modules[__name__] = _module
