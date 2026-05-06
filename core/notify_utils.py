"""Shared notification utilities for core modules.

Extracts _telegram_direct from agents/notify.py so that core/ modules
(kg_guards) can send Telegram messages without importing from agents/.
"""

import logging

from core.secrets import get_credential

logger = logging.getLogger(__name__)

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]


def telegram_direct(message: str) -> tuple[bool, str]:
    """Send via Telegram Bot API directly (bypasses gateway).

    Args:
        message: Text message to send to the owner.

    Returns:
        Tuple of (success, detail_message).
    """
    if httpx is None:
        return False, "httpx not installed"

    token = get_credential("TELEGRAM_BOT_TOKEN")
    chat_id = get_credential("TELEGRAM_OWNER_CHAT_ID")

    if not token or not chat_id:
        return False, "TELEGRAM_BOT_TOKEN or TELEGRAM_OWNER_CHAT_ID not set"

    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        if resp.status_code == 200:
            return True, "delivered via Telegram"
        return False, f"Telegram API returned {resp.status_code}"
    except Exception as e:
        return False, f"Telegram error: {type(e).__name__}"
