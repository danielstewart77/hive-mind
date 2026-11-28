"""`import auth` — the shared module, bound here under its bare name.

This file used to be 1243 lines, a verbatim copy of the console's `auth.py`
that had stopped being verbatim: it opened `hive.db` with `sqlite3` by hand
and minted its own signing key into that file, while the console moved onto
a store chosen by `HIVE_DATABASE_URL`. Those are the two halves of one
account store disagreeing, and what it looks like from a chair is being
signed out of the terminal and nowhere else.

So there is no copy any more. `hive_site` is installed into this image from
a build context, exactly as `hive_data` is, and this binds its module into
`sys.modules` under the bare name — three lines rather than a re-export, so
`import auth` *is* `hive_site.auth` and a test that patches
`auth.get_store` patches the thing the routes call.
"""

import sys

from hive_site import auth as _module

sys.modules[__name__] = _module
