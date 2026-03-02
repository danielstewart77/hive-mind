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

    def resolve(self, token: str, approved: bool) -> bool:
        """Resolve a pending confirmation. Returns False if token is invalid/expired."""
        entry = self._pending.pop(token, None)
        if entry is None:
            log.warning("HITL resolve: unknown token %s", token)
            return False
        if time.time() > entry.expires_at:
            log.warning("HITL resolve: expired token %s", token)
            return False
        entry.approved = approved
        entry.event.set()
        log.info("HITL resolved: token=%s approved=%s action=%s", token, approved, entry.action)
        return True

    def cleanup_expired(self):
        """Wake any waiters on expired tokens and purge them."""
        now = time.time()
        expired = [t for t, e in self._pending.items() if now > e.expires_at]
        for token in expired:
            entry = self._pending.pop(token)
            entry.approved = None
            entry.event.set()  # unblock the waiter
            log.info("HITL expired: token=%s action=%s", token, entry.action)


hitl_store = HITLStore()
