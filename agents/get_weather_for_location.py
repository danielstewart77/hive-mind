from typing import Optional, List, Dict, Generator
from agent_tooling import tool
import requests
from datetime import datetime, timedelta
from utilities.openai_tools import completions_streaming

class WeatherAgent:
    def __init__(self, location: str = "missouri city, tx", time_span: str = "today"):
        self.location = location
        self.time_span = time_span.lower()
        self.api_key = ""  # No API key needed for open-meteo

    def get_coordinates(self) -> tuple[float, float]:
        # Use Nominatim for geocoding
        url = f"https://nominatim.openstreetmap.org/search"
        params = {
            "q": self.location,
            "format": "json",
            "limit": 1
        }
        response = requests.get(url, params=params, headers={"User-Agent": "hivemind-weather-agent"})
        if response.status_code != 200 or not response.text.strip():
            raise RuntimeError(f"Nominatim request failed: {response.status_code} - {response.text}")

        try:
            data = response.json()
        except requests.exceptions.JSONDecodeError:
            raise RuntimeError("Nominatim returned invalid JSON.")

        if not data:
            raise ValueError("Location not found.")
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
        else:
            raise ValueError("Location not found")

    def get_weather(self) -> str:
        lat, lon = self.get_coordinates()

        # Define the time span
        today = datetime.utcnow().date()

        if self.time_span == "today":
            start_date = today
            end_date = today
        elif self.time_span == "tonight":
            start_date = today
            end_date = today
        elif self.time_span == "this week":
            # From today to next Sunday
            today_weekday = today.weekday()  # Monday = 0
            end_date = today + timedelta(days=(6 - today_weekday))
            start_date = today
        elif self.time_span == "this weekend":
            # Saturday and Sunday of this week (assuming week start Monday)
            today_weekday = today.weekday()
            saturday = today + timedelta(days=(5 - today_weekday))
            sunday = today + timedelta(days=(6 - today_weekday))
            start_date = saturday
            end_date = sunday
        else:
            # Default to today
            start_date = today
            end_date = today

        start_str = start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")

        # We'll call Open-Meteo API for daily weather
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": start_str,
            "end_date": end_str,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode",
            "timezone": "auto"
        }

        response = requests.get(url, params=params)
        data = response.json()

        if "daily" not in data:
            return "Weather information is not available for the given location/time."

        days_weather = []
        daily = data["daily"]
        days = daily["time"]
        temps_max = daily["temperature_2m_max"]
        temps_min = daily["temperature_2m_min"]
        precipitation = daily["precipitation_sum"]
        weathercodes = daily["weathercode"]

        weathercode_meaning = {
            0: "Clear sky",
            1: "Mainly clear",
            2: "Partly cloudy",
            3: "Overcast",
            45: "Fog",
            48: "Depositing rime fog",
            51: "Light drizzle",
            53: "Moderate drizzle",
            55: "Dense drizzle",
            56: "Light freezing drizzle",
            57: "Dense freezing drizzle",
            61: "Slight rain",
            63: "Moderate rain",
            65: "Heavy rain",
            66: "Light freezing rain",
            67: "Heavy freezing rain",
            71: "Slight snow fall",
            73: "Moderate snow fall",
            75: "Heavy snow fall",
            77: "Snow grains",
            80: "Slight rain showers",
            81: "Moderate rain showers",
            82: "Violent rain showers",
            85: "Slight snow showers",
            86: "Heavy snow showers",
            95: "Thunderstorm",
            96: "Thunderstorm with slight hail",
            99: "Thunderstorm with heavy hail"
        }

        for day, tmax, tmin, prcp, wcode in zip(days, temps_max, temps_min, precipitation, weathercodes):
            desc = weathercode_meaning.get(wcode, "Unknown")
            days_weather.append(f"{day}: {desc}, Max Temp: {tmax}\u00b0C, Min Temp: {tmin}\u00b0C, Precipitation: {prcp}mm")

        if self.time_span == "tonight":
            # Just report today's weather in a concise way
            return f"Tonight in {self.location.title()}: {days_weather[0]}"
        elif start_date == end_date:
            return f"Weather for {self.location.title()} on {start_str}: {days_weather[0]}"
        else:
            return f"Weather for {self.location.title()} from {start_str} to {end_str}:\n" + "\n".join(days_weather)

@tool(tags=["agent"])
def get_weather_for_location(location: str = "missouri city, tx", time_span: str = "today", messages: Optional[List[Dict[str, str]]] = None) -> Generator[str, None, None]:
    """
    Retrieves weather information for a specified location and time span, streaming the response.

    Parameters:
    - location (str): The place to get weather for, e.g., "missouri city, tx".
    - time_span (str): The period to get weather for, default is "today". Options include "today", "tonight", "this week", and "this weekend".
    - messages (list): Optional parameter for message context, not used in the current implementation.

    Yields:
    - str: Chunks of a nicely formatted weather report.
    """
    # Validate and convert arguments if necessary

    if not isinstance(location, str):
        try:
            location = str(location)
        except Exception:
            yield "Invalid location parameter provided."
            return

    if not isinstance(time_span, str):
        try:
            time_span = str(time_span)
        except Exception:
            time_span = "today"

    if messages is not None and not isinstance(messages, list):
        try:
            messages = list(messages)
        except Exception:
            messages = None

    agent = WeatherAgent(location, time_span)
    result = agent.get_weather()

    # Compose a message for LLM to format nicely
    message_to_format = f"Provide a friendly weather report for the following data:\n{result}"
    stream = completions_streaming(message=message_to_format)

    for chunk in stream:
        yield chunk

# Example usage as generator:
# for chunk in get_weather_for_location("new york, ny", "this weekend"):
#     print(chunk, end='')
