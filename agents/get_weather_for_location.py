import json
import requests
from datetime import datetime, timedelta
from agent_tooling import tool


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
    today = datetime.utcnow().date()
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


@tool(tags=["weather"])
def get_weather_for_location(location: str = "missouri city, tx", time_span: str = "today") -> str:
    """Get weather forecast for a location. Returns raw weather data as JSON.

    Args:
        location: Place name (e.g. "new york, ny", "london, uk")
        time_span: "today", "tonight", "this week", or "this weekend"

    Returns:
        JSON string with forecast data including location, dates, temperatures, conditions.
    """
    lat, lon = _geocode(location)
    start_str, end_str = _date_range(time_span)

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
        return json.dumps({"error": "No weather data available for this location/time."})

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

    return json.dumps({
        "location": location,
        "time_span": time_span,
        "timezone": data.get("timezone", "UTC"),
        "forecast": days,
    })
