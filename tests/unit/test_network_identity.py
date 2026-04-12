"""Unit tests for core/network_identity.py -- Docker DNS reverse lookup.

Tests the resolve_container_name() function for identifying callers
by their Docker network IP address.
"""

import asyncio
import socket
from unittest.mock import patch

import pytest


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestResolveContainerName:
    """Tests for resolve_container_name()."""

    def test_resolve_container_name_returns_hostname(self):
        """Successful reverse DNS returns the container hostname."""
        from core.network_identity import resolve_container_name

        with patch("socket.gethostbyaddr", return_value=("bilby", [], ["172.18.0.5"])):
            result = _run(resolve_container_name("172.18.0.5"))
        assert result == "bilby"

    def test_resolve_container_name_returns_none_on_failure(self):
        """DNS lookup failure returns None."""
        from core.network_identity import resolve_container_name

        with patch("socket.gethostbyaddr", side_effect=socket.herror("not found")):
            result = _run(resolve_container_name("172.18.0.99"))
        assert result is None

    def test_resolve_container_name_strips_domain_suffix(self):
        """Docker DNS may return FQDN; strip everything after first dot."""
        from core.network_identity import resolve_container_name

        with patch("socket.gethostbyaddr", return_value=("bilby.hivemind", [], ["172.18.0.5"])):
            result = _run(resolve_container_name("172.18.0.5"))
        assert result == "bilby"

    def test_resolve_container_name_returns_none_on_gaierror(self):
        """socket.gaierror also returns None."""
        from core.network_identity import resolve_container_name

        with patch("socket.gethostbyaddr", side_effect=socket.gaierror("name resolution failed")):
            result = _run(resolve_container_name("172.18.0.99"))
        assert result is None

    def test_resolve_container_name_returns_none_on_oserror(self):
        """Generic OSError also returns None."""
        from core.network_identity import resolve_container_name

        with patch("socket.gethostbyaddr", side_effect=OSError("generic error")):
            result = _run(resolve_container_name("172.18.0.99"))
        assert result is None
