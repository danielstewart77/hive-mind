#!/usr/bin/env python3
"""Poll the broker for an inter-mind task result.

Standalone stateless tool. Called by the poll-task-result agent.
Polls GET /broker/messages every 30 seconds, checks for callee response.
Exits 0 with JSON on result, exits 1 with JSON on timeout.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Notification thresholds (seconds)
# ---------------------------------------------------------------------------
THRESHOLDS: dict[str, int] = {
    "quick_query": 300,           # 5 min
    "research": 1200,             # 20 min
    "code_review": 1200,          # 20 min
    "content_generation": 900,    # 15 min
    "data_analysis": 1200,        # 20 min
    "security_triage": 1800,      # 30 min
    "security_remediation": 5400, # 90 min
}

_DEFAULT_THRESHOLD = 1200  # 20 min


def get_threshold(request_type: str) -> int:
    """Get the notification threshold in seconds for a request_type."""
    return THRESHOLDS.get(request_type, _DEFAULT_THRESHOLD)


def get_hard_ceiling(request_type: str) -> int:
    """Get the hard ceiling (4x threshold) in seconds."""
    return get_threshold(request_type) * 4


# ---------------------------------------------------------------------------
# Result checking
# ---------------------------------------------------------------------------
def check_for_result(gateway_url: str, conversation_id: str, to_mind: str) -> dict | None:
    """Check if the callee has responded.

    Returns the response message dict if found, None otherwise.
    Only considers messages with status='completed' from the callee.
    """
    url = f"{gateway_url}/broker/messages?" + urllib.parse.urlencode({"conversation_id": conversation_id})
    req = urllib.request.urlopen(url, timeout=10)
    messages = json.loads(req.read().decode())

    for msg in messages:
        if msg.get("from_mind") == to_mind and msg.get("status") == "completed":
            return msg
    return None


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------
def build_notification_message(
    request_type: str,
    threshold: int,
    conversation_id: str,
) -> str:
    """Build a notification message for threshold exceeded."""
    return (
        f"Inter-mind task [{request_type}] has exceeded its expected threshold "
        f"of {threshold}s ({threshold // 60} min). "
        f"Conversation: {conversation_id}. Still polling."
    )


def send_notification(message: str) -> None:
    """Send a notification via the notify tool. Falls back to stderr."""
    notify_script = os.path.join(
        os.path.dirname(__file__), "..", "notify", "notify.py"
    )
    if os.path.exists(notify_script):
        try:
            subprocess.run(
                [sys.executable, notify_script, "--channel", "telegram", "--message", message],
                timeout=30,
                capture_output=True,
            )
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Poll broker for inter-mind result")
    parser.add_argument("--conversation_id", required=True)
    parser.add_argument("--from_mind", required=True)
    parser.add_argument("--to_mind", required=True)
    parser.add_argument("--request_type", required=True)
    parser.add_argument("--gateway_url", default="http://localhost:8420")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def _is_daytime() -> bool:
    """Check if current time is between 6am and 10pm local."""
    now = datetime.now(ZoneInfo("America/Chicago"))
    return 6 <= now.hour < 22


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    threshold = get_threshold(args.request_type)
    ceiling = get_hard_ceiling(args.request_type)
    start = time.monotonic()
    poll_interval = 30
    threshold_exceeded = False

    while True:
        try:
            result = check_for_result(args.gateway_url, args.conversation_id, args.to_mind)
        except Exception as e:
            print(f"Poll error: {e}", file=sys.stderr)
            result = None

        if result is not None:
            print(json.dumps({
                "status": "completed",
                "response": result.get("content", ""),
                "conversation_id": args.conversation_id,
                "from_mind": result.get("from_mind", args.to_mind),
            }))
            return 0

        elapsed = time.monotonic() - start

        # Hard ceiling — give up
        if elapsed >= ceiling:
            print(json.dumps({
                "status": "timeout",
                "conversation_id": args.conversation_id,
                "elapsed_seconds": int(elapsed),
                "request_type": args.request_type,
            }))
            return 1

        # Threshold exceeded — start notifying
        if elapsed >= threshold and not threshold_exceeded:
            threshold_exceeded = True
            msg = build_notification_message(args.request_type, threshold, args.conversation_id)
            send_notification(msg)
            # Switch to slower poll interval
            poll_interval = 180 if _is_daytime() else 14400  # 3 min or 4 hr

        elif threshold_exceeded:
            # Periodic re-notification
            msg = build_notification_message(args.request_type, threshold, args.conversation_id)
            send_notification(msg)

        time.sleep(poll_interval)


if __name__ == "__main__":
    sys.exit(main())
