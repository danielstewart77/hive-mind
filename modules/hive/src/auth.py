"""`import auth` — the shared module, bound here under its bare name.

Three lines rather than a re-export. The console's routes and its suite
reach this module as `auth`, and its tests patch names on it; a
`from hive_site.auth import *` would bind copies of those names, so a
patched `auth.get_store` would not be the one the module's own code calls.
Binding the module object itself into `sys.modules` means `import auth` *is*
`hive_site.auth`, so there is one object to patch and no local file that can
drift from the terminal site's.
"""

import sys
from typing import TYPE_CHECKING

from hive_site import auth as _module

sys.modules[__name__] = _module

if TYPE_CHECKING:
    # Only for the type checker, which cannot follow a module swapped into
    # `sys.modules` and would report every name this site reads through
    # `auth.` as missing. Never executed: at runtime the line above has
    # already replaced this module with the real one.
    from hive_site.auth import *  # noqa: F401,F403
