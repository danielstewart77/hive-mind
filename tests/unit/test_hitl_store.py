"""Unit tests for HITLStore — specifically cleanup_expired() returning tokens."""

import time

from core.hitl import HITLStore


class TestCleanupExpiredReturnsTokens:
    """Tests for cleanup_expired() returning a list of expired token strings."""

    def test_cleanup_expired_returns_empty_list_when_no_expired(self) -> None:
        """Non-expired entries should not appear in the return value."""
        store = HITLStore()
        token, _entry = store.create("test_action", "test summary", ttl=300)
        result = store.cleanup_expired()
        assert result == []

    def test_cleanup_expired_returns_expired_tokens(self) -> None:
        """Expired, unresolved tokens should be returned."""
        store = HITLStore()
        token, entry = store.create("test_action", "test summary", ttl=0)
        # Force expiry by setting expires_at in the past
        entry.expires_at = time.time() - 1
        result = store.cleanup_expired()
        assert token in result

    def test_cleanup_expired_returns_only_unresolved_tokens(self) -> None:
        """Resolved entries that expired should not appear in the return value."""
        store = HITLStore()
        token_resolved, entry_resolved = store.create("action1", "resolved one", ttl=0)
        entry_resolved.expires_at = time.time() - 1
        entry_resolved.approved = True  # already resolved

        token_pending, entry_pending = store.create("action2", "pending one", ttl=0)
        entry_pending.expires_at = time.time() - 1
        # entry_pending.approved is None — still pending

        result = store.cleanup_expired()
        assert token_pending in result
        assert token_resolved not in result

    def test_cleanup_expired_wakes_waiters(self) -> None:
        """Expired pending entries should have their event set."""
        store = HITLStore()
        token, entry = store.create("action", "summary", ttl=0)
        entry.expires_at = time.time() - 1
        store.cleanup_expired()
        assert entry.event.is_set()

    def test_hitl_status_reports_expired(self) -> None:
        """A token with TTL=0 should report state=expired."""
        store = HITLStore()
        token, entry = store.create("action", "summary", ttl=0)
        entry.expires_at = time.time() - 1
        status = store.status(token)
        assert status["state"] == "expired"

    def test_hitl_resolve_rejects_expired(self) -> None:
        """resolve() should return False for expired tokens."""
        store = HITLStore()
        token, entry = store.create("action", "summary", ttl=0)
        entry.expires_at = time.time() - 1
        result = store.resolve(token, True)
        assert result is False
