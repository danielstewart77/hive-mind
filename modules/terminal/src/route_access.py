"""`import route_access` — the shared module, bound here under its bare name.

The authorization middleware in front of every route and websocket, held in
`hive_site` with the account store rather than copied into both sites. It
was byte-identical here and in the console, which is the same hazard the
account store just stopped being: one of the two gets a fix and the other
keeps the hole. Which routes it enforces is still this site's own
`terminal_access.TABLE`, passed in when the middleware is added.
"""

import sys

from hive_site import route_access as _module

sys.modules[__name__] = _module
