"""Unit tests for lucent_api.auth bearer dependencies.

Verifies that:
- require_bearer accepts the regular token and the admin token (admin is
  strictly more privileged, used by hive-tools when calling admin routes
  that live inside the graph router).
- require_bearer rejects unrelated tokens.
- require_admin_bearer accepts ONLY the admin token (no elevation
  shortcut — the elevation barrier holds the other way).
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from fastapi import HTTPException

from lucent_api.auth import require_admin_bearer, require_bearer


REGULAR = "regular-token-abc"
ADMIN = "admin-token-xyz"


class RequireBearerTests(unittest.TestCase):
    @mock.patch.dict(
        os.environ,
        {"LUCENT_BEARER_TOKEN": REGULAR, "LUCENT_ADMIN_BEARER_TOKEN": ADMIN},
    )
    def test_accepts_regular_token(self):
        # No exception → pass
        require_bearer(authorization=f"Bearer {REGULAR}")

    @mock.patch.dict(
        os.environ,
        {"LUCENT_BEARER_TOKEN": REGULAR, "LUCENT_ADMIN_BEARER_TOKEN": ADMIN},
    )
    def test_accepts_admin_token(self):
        # Admin token must pass the regular gate so admin routes nested
        # inside the graph router (which has require_bearer at router level)
        # don't 401 before reaching the admin dep.
        require_bearer(authorization=f"Bearer {ADMIN}")

    @mock.patch.dict(
        os.environ,
        {"LUCENT_BEARER_TOKEN": REGULAR, "LUCENT_ADMIN_BEARER_TOKEN": ADMIN},
    )
    def test_rejects_unrelated_token(self):
        with self.assertRaises(HTTPException) as ctx:
            require_bearer(authorization="Bearer some-other-token")
        self.assertEqual(ctx.exception.status_code, 401)

    @mock.patch.dict(os.environ, {"LUCENT_BEARER_TOKEN": REGULAR}, clear=True)
    def test_admin_unset_does_not_widen_access(self):
        # When LUCENT_ADMIN_BEARER_TOKEN is unset, only the regular token works.
        require_bearer(authorization=f"Bearer {REGULAR}")
        with self.assertRaises(HTTPException):
            require_bearer(authorization="Bearer anything-else")

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_bypass_when_regular_unset(self):
        # If LUCENT_BEARER_TOKEN is unset, the gate is wide open (deployment
        # bootstrap safety). Any header passes.
        require_bearer(authorization="")
        require_bearer(authorization="Bearer literally-anything")


class RequireAdminBearerTests(unittest.TestCase):
    @mock.patch.dict(
        os.environ,
        {"LUCENT_BEARER_TOKEN": REGULAR, "LUCENT_ADMIN_BEARER_TOKEN": ADMIN},
    )
    def test_accepts_admin_token(self):
        require_admin_bearer(authorization=f"Bearer {ADMIN}")

    @mock.patch.dict(
        os.environ,
        {"LUCENT_BEARER_TOKEN": REGULAR, "LUCENT_ADMIN_BEARER_TOKEN": ADMIN},
    )
    def test_regular_token_NOT_elevated(self):
        # Elevation only flows admin → regular, NEVER the reverse.
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
