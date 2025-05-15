from agent_tooling import tool
import requests
import json
from dotenv import load_dotenv
load_dotenv(dotenv_path='secrets.env')
import os
from openai import OpenAI

VISUAL_CROSSING_API_KEY = os.getenv('VISUAL_CROSSING_API_KEY')
BASE_URL = 'https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline/'

@tool(tags=["triage"])
def get_houston_weather_forecast(messages: list[dict[str, str]], location: str = 'Houston,TX') -> str:
    """
    Responds to a request for the weather forecast.

    Args:
        location (str): The location for which the weather forecast is requested. Defaults to 'Houston,TX'.
        messages list[dict[str, str]]: The message thread sent to the agent.

    Returns:
        str: A formatted string containing the weather forecast details for the specified
    """
    yield "Fetching weather forecast from Visual Crossing...\n\n\n"
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
    client = OpenAI(api_key=OPENAI_API_KEY)

    location = 'Houston,TX'
    url = f"{BASE_URL}{location}?key={VISUAL_CROSSING_API_KEY}"
    
    try:
        response = requests.get(url)
        response.raise_for_status()  # Raise an error if the request was unsuccessful.
        data = response.json()
        weather_data = parse_weather_data(data)

        # create a new message thread of type list[dict[str, str]]
        messages = [
            {
            "role": "system",
            "content": f"Weather forecast for {location}:\n{weather_data}"
            },
            {
            "role": "system",
            "content": "Please ensure your answer incorporates the content from the tool output above."
            }
        ]
            
        stream = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            stream=True
        )

        response_text = ""
        for chunk in stream:
            if chunk.choices[0].delta.content is not None:
                content = chunk.choices[0].delta.content
                response_text += content
                # Wrap in JSON for consistent format
                yield content

    except Exception as err:
        return f'Other error occurred: {err}'

def parse_weather_data(data) -> str:
    
    if not data or 'days' not in data:
        return 'No weather data found.'
    
    forecast_details = []
    for day in data['days']:
        date = day.get('datetime', 'N/A')
        conditions = day.get('conditions', 'N/A')
        temp_max = day.get('tempmax', 'N/A')
        temp_min = day.get('tempmin', 'N/A')
        precipitation = day.get('precipprob', 'N/A')
        humidity = day.get('humidity', 'N/A')

        forecast_details.append(
            f"Date: {date}\n"
            f"Conditions: {conditions}\n"
            f"Temperature: Max {temp_max}°F / Min {temp_min}°F\n"
            f"Precipitation Probability: {precipitation}%\n"
            f"Humidity: {humidity}%\n"
        )

    return "\n\n".join(forecast_details)