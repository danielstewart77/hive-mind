"""`import accounts` — the shared test helpers, under the name this suite uses.

They moved into `hive_site.testing` when the account store did, so the
terminal site's suite uses the same ones rather than a copy. Bound here
under the bare name so the thirty-odd modules that `import accounts` are
unchanged.
"""

import sys

from hive_site import testing as _module

sys.modules[__name__] = _module
