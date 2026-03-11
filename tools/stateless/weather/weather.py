#!/usr/bin/env python3
"""Get weather forecast for a location.

Standalone stateless tool. Dependencies: requests.
"""

import argparse
import json
import sys
import os
from datetime import datetime, timedelta, timezone

# Allow importing core.secrets
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

WEATHERCODE_MEANING = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow fall", 73: "Moderate snow fall", 75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}


def _geocode(location: str) -> tuple[float, float]:
    import requests
    resp = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": location, "format": "json", "limit": 1},
        headers={"User-Agent": "hivemind-weather-agent"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"Location not found: {location}")
    return float(data[0]["lat"]), float(data[0]["lon"])


def _date_range(time_span: str) -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    span = time_span.lower()
    if span in ("today", "tonight"):
        start, end = today, today
    elif span == "this week":
        start = today
        end = today + timedelta(days=(6 - today.weekday()))
    elif span == "this weekend":
        start = today + timedelta(days=(5 - today.weekday()))
        end = today + timedelta(days=(6 - today.weekday()))
    else:
        start, end = today, today
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _mock_forecast(location: str, time_span: str) -> dict:
    """Return mock forecast data for testing."""
    today = datetime.now(timezone.utc).date()
    start_str, end_str = _date_range(time_span)
    start = datetime.strptime(start_str, "%Y-%m-%d").date()
    end = datetime.strptime(end_str, "%Y-%m-%d").date()
    days = []
    current = start
    while current <= end:
        days.append({
            "date": current.strftime("%Y-%m-%d"),
            "condition": "Partly cloudy",
            "temp_max_c": 28.5,
            "temp_min_c": 18.2,
            "precipitation_mm": 0.0,
        })
        current += timedelta(days=1)
    return {
        "location": location,
        "time_span": time_span,
        "timezone": "America/Chicago",
        "forecast": days,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Get weather forecast")
    parser.add_argument("--location", default="missouri city, tx", help="City and state/country")
    parser.add_argument("--time-span", default="today", help="today, tonight, this week, this weekend")
    parser.add_argument("--test-mode", action="store_true", help="Use mock data")
    args = parser.parse_args()

    if args.test_mode:
        print(json.dumps(_mock_forecast(args.location, args.time_span)))
        return 0

    try:
        import requests
        lat, lon = _geocode(args.location)
        start_str, end_str = _date_range(args.time_span)

        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "start_date": start_str, "end_date": end_str,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode",
                "timezone": "auto",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if "daily" not in data:
            print(json.dumps({"error": "No weather data available for this location/time."}))
            return 1

        daily = data["daily"]
        days = []
        for date, tmax, tmin, precip, wcode in zip(
            daily["time"], daily["temperature_2m_max"], daily["temperature_2m_min"],
            daily["precipitation_sum"], daily["weathercode"],
        ):
            days.append({
                "date": date,
                "condition": WEATHERCODE_MEANING.get(wcode, "Unknown"),
                "temp_max_c": tmax,
                "temp_min_c": tmin,
                "precipitation_mm": precip,
            })

        print(json.dumps({
            "location": args.location,
            "time_span": args.time_span,
            "timezone": data.get("timezone", "UTC"),
            "forecast": days,
        }))
        return 0

    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
