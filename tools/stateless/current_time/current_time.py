#!/usr/bin/env python3
"""Get the current date and time.

Standalone stateless tool. No external dependencies (stdlib only).
"""

import argparse
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo


def main() -> int:
    parser = argparse.ArgumentParser(description="Get current date and time")
    parser.add_argument(
        "--timezone",
        # The machine's zone, so an installed hive answers in its own time.
        default=os.environ.get("TZ", "").strip() or "America/Chicago",
        help="IANA timezone (default: $TZ, else America/Chicago)",
    )
    args = parser.parse_args()

    try:
        tz = ZoneInfo(args.timezone)
        now = datetime.now(tz)
        print(json.dumps({
            "time": now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z"),
            "timezone": args.timezone,
        }))
        return 0
    except Exception:
        print(json.dumps({"error": f"Invalid timezone: {args.timezone}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
