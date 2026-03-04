from datetime import datetime
from zoneinfo import ZoneInfo

from agent_tooling import tool


@tool(tags=["utility"])
def get_current_time(timezone: str = "America/Chicago") -> str:
    """Returns the current date and time with day of week. Call this whenever
    time-sensitive language appears mid-conversation (today, now, tonight,
    this morning, this week, etc.) to ensure temporal accuracy."""
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    return now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")
