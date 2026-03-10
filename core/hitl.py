"""
Hive Mind — Human-in-the-Loop (HITL) approval store.

In-memory token store with asyncio.Event-based waiting.
MCP tools POST to /hitl/request, block until the owner
approves or denies via Telegram, then get a yes/no back.
"""

import asyncio
import logging
import secrets
import time

log = logging.getLogger("hive-mind.hitl")

DEFAULT_TTL = 180  # seconds before a pending request expires


class PendingConfirmation:
    __slots__ = ("action", "summary", "ttl", "expires_at", "event", "approved")

    def __init__(self, action: str, summary: str, ttl: int = DEFAULT_TTL):
        self.action = action
        self.summary = summary
        self.ttl = ttl
        self.expires_at = time.time() + ttl
        self.event = asyncio.Event()
        self.approved: bool | None = None


class HITLStore:
    def __init__(self):
        self._pending: dict[str, PendingConfirmation] = {}

    def create(self, action: str, summary: str, ttl: int = DEFAULT_TTL) -> tuple[str, PendingConfirmation]:
        """Create a new pending confirmation and return (token, entry)."""
        token = secrets.token_hex(6)  # 12 chars — fits Telegram command limit
        entry = PendingConfirmation(action, summary, ttl=ttl)
        self._pending[token] = entry
        log.info("HITL created: token=%s action=%s ttl=%ds summary=%r", token, action, ttl, summary[:100])
        return token, entry

    def status(self, token: str) -> dict:
        """Check the status of a pending confirmation (non-blocking).

        Returns dict with 'state': 'pending' | 'approved' | 'denied' | 'expired' | 'unknown'.
        """
        entry = self._pending.get(token)
        if entry is None:
            return {"state": "unknown"}
        if time.time() > entry.expires_at:
            return {"state": "expired"}
        if entry.approved is True:
            return {"state": "approved"}
        if entry.approved is False:
            return {"state": "denied"}
        return {"state": "pending"}

    def resolve(self, token: str, approved: bool) -> bool:
        """Resolve a pending confirmation. Returns False if token is invalid/expired.

        The entry stays in _pending so status() can still read the result.
        cleanup_expired() handles removal.
        """
        entry = self._pending.get(token)
        if entry is None:
            log.warning("HITL resolve: unknown token %s", token)
            return False
        if time.time() > entry.expires_at:
            log.warning("HITL resolve: expired token %s", token)
            self._pending.pop(token, None)
            return False
        entry.approved = approved
        entry.event.set()
        log.info("HITL resolved: token=%s approved=%s action=%s", token, approved, entry.action)
        return True

    def cleanup_expired(self) -> list[str]:
        """Wake any waiters on expired tokens and purge stale entries.

        Resolved entries are kept until their TTL expires so polling
        clients can still read the result via status().

        Returns the list of tokens that were pending (unresolved) at expiry time.
        """
        now = time.time()
        expired = [t for t, e in self._pending.items() if now > e.expires_at]
        pending_expired: list[str] = []
        for token in expired:
            entry = self._pending.pop(token)
            if entry.approved is None:
                entry.event.set()  # unblock the waiter
                pending_expired.append(token)
                log.info("HITL expired: token=%s action=%s", token, entry.action)
        return pending_expired


hitl_store = HITLStore()
