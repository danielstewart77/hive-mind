"""`import store` — the shared module, bound here under its bare name.

Three lines rather than a re-export. The console's routes and its suite
reach this module as `store`, and sixty test files patch names on it; a
`from hive_site.store import *` would bind copies of those names, so a
patched `store.open` would not be the `open` the package's own code calls.
Binding the module object itself into `sys.modules` means `import store`
*is* `hive_site.store`, so there is one object to patch and no local file
that can drift from the terminal's.
"""

import sys
from typing import TYPE_CHECKING

from hive_site import store as _module

sys.modules[__name__] = _module

if TYPE_CHECKING:
    # Only for the type checker, which cannot follow a module swapped into
    # `sys.modules` and would report every name this site reads through
    # `store.` as missing. Never executed: at runtime the line above has
    # already replaced this module with the real one.
    from hive_site.store import *  # noqa: F401,F403
