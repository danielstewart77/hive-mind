"""Unit tests for the two credentials lucent still reads from the environment.

`require_bearer` and the shared `LUCENT_BEARER_TOKEN` it checked are gone:
that was one value held by every mind and every hook on every host. The tests
that asserted it worked are deleted rather than patched, and so is the one
that asserted an unset value served every caller — per-holder credentials and
the refusal to serve unconfigured are covered in `test_holder_access.py`.

What is left here is the admin credential, which stays a single shared value
because it is how the console reaches the store and the only way the minting
route is reachable on a first boot, and the soul credential, which was already
held by exactly one holder by design.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from fastapi import HTTPException

from lucent_api.auth import require_admin_bearer


REGULAR = "regular-token-abc"
ADMIN = "admin-token-xyz"


class RequireAdminBearerTests(unittest.TestCase):
    @mock.patch.dict(
        os.environ,
        {"LUCENT_ADMIN_BEARER_TOKEN": ADMIN},
    )
    def test_accepts_admin_token(self):
        require_admin_bearer(authorization=f"Bearer {ADMIN}")

    @mock.patch.dict(
        os.environ,
        {"LUCENT_ADMIN_BEARER_TOKEN": ADMIN},
    )
    def test_another_value_is_not_elevated(self):
        """Nothing is elevated into the admin credential — not the value the
        retired service token used to hold, and not a holder's own."""
        with self.assertRaises(HTTPException) as ctx:
            require_admin_bearer(authorization=f"Bearer {REGULAR}")
        self.assertEqual(ctx.exception.status_code, 401)

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_unset_admin_returns_503(self):
        # No bypass on admin — locked rather than open.
        with self.assertRaises(HTTPException) as ctx:
            require_admin_bearer(authorization=f"Bearer {ADMIN}")
        self.assertEqual(ctx.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
