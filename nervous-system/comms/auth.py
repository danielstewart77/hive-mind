"""Bearer-token auth for comms.

Mirrors ``lucent_api/auth.py`` (same env-var shape, same logic). Two
tokens:

- ``COMMS_BEARER_TOKEN`` — service token. Required for every route
  except health/root. Empty/unset = bypass mode (with a startup
  warning), so a fresh container can come up before the operator
  finishes wiring secrets.

There is no admin token: an administrator acts with their own person key,
which the per-holder gate in `user_access.UserAccess` resolves.
"""

from __future__ import annotations

import logging
import os


log = logging.getLogger(__name__)

# Endpoints with their own request-level auth (HMAC, signature, etc.) that
# would be incorrectly gated by the global bearer middleware.
BEARER_EXEMPT_PATHS = frozenset({"/sms/inbound"})
