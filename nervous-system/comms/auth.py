"""What comms takes as a credential, and what it does not.

Every request is judged by the per-holder gate in `user_access` against the
hive database's one record of keys, read on every request: a person's own key,
a mind's own key, or a `service` holder's key where one has been minted. The
old shared ``COMMS_BEARER_TOKEN`` is not accepted, and there is no administrator
token and no bypass mode: a request that presents nothing the record knows is
refused. This module keeps only the paths that carry a credential of their own
(an HMAC-signed webhook), which the bearer gate would wrongly refuse.
"""

from __future__ import annotations

import logging
import os


log = logging.getLogger(__name__)

# Endpoints with their own request-level auth (HMAC, signature, etc.) that
# would be incorrectly gated by the global bearer middleware.
BEARER_EXEMPT_PATHS = frozenset({"/sms/inbound"})
