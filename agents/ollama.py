import os
import re
import httpx
import requests
import json
from typing import List, Dict, Type, TypeVar
from pydantic import BaseModel, ValidationError

from dotenv import load_dotenv
load_dotenv(dotenv_path='secrets.env')

OLLAMA_API_ADDRESS = os.getenv('OLLAMA_API_ADDRESS')
OLLAMA_API_URL = f'http://{OLLAMA_API_ADDRESS}:11434/api'

# Define Pydantic models for the task decomposition
class StepDescription(BaseModel):
    step: int
    description: List[str]

class TaskDecomposition(BaseModel):
    steps: List[StepDescription]

def ollama_generate(message: str, llm_model: str, instruction: str) -> str:
    
    # Prepare the request payload for the Ollama API with JSON mode
    payload = {
        "model": llm_model,
        "prompt": f"{instruction}\n\nTask: {message}",
        "stream": False
    }

    response = requests.post(f'{OLLAMA_API_URL}/generate', json=payload)
    
    if response.status_code == 200:
        json_response = response.json()
        raw_response = json_response['response']
        try:
            clean_response = re.sub(r'^```(?:json)?\n|\n```$', '', raw_response.strip())
            return clean_response
        
        except json.JSONDecodeError as e:
            print(f"Failed to parse JSON: {str(e)}")
            print(f"Raw response: {raw_response}")
            raise
        except Exception as e:
            print(f"Failed to validate with Pydantic: {str(e)}")
            raise
    else:
        raise Exception(f"API request failed with status code {response.status_code}")
    
    
def ollama_generate(message: str, llm_model: str, instruction: str, data_model: BaseModel) -> BaseModel:
    
    # Prepare the request payload for the Ollama API with JSON mode
    payload = {
        "model": llm_model,
        "prompt": f"{instruction}\n\nTask: {message}",
        "stream": False,
        "options": {
            "response_format": {"type": "json_object"}
        }
    }

    response = requests.post(f'{OLLAMA_API_URL}/generate', json=payload)
    
    if response.status_code == 200:
        json_response = response.json()
        raw_response = json_response['response']
        try:
            clean_response = re.sub(r'^```(?:json)?\n|\n```$', '', raw_response.strip())
            
            # Parse the JSON response
            json_result = json.loads(clean_response)
            # Convert to Pydantic model for validation and type safety
            
            model = data_model(**json_result)
        
            return model
        except json.JSONDecodeError as e:
            print(f"Failed to parse JSON: {str(e)}")
            print(f"Raw response: {raw_response}")
            raise
        except Exception as e:
            print(f"Failed to validate with Pydantic: {str(e)}")
            raise
    else:
        raise Exception(f"API request failed with status code {response.status_code}")
    


T = TypeVar("T", bound=BaseModel)

def ollama_structured_chat(
    messages: List[Dict[str, str]],
    model: str,
    response_format: Type[T]
) -> T:
    try:
        response = httpx.post(
            f"{OLLAMA_API_URL}/api/chat",
            json={
                "model": model,
                "messages": messages,
                "stream": False
            },
            timeout=30
        )
        response.raise_for_status()
        content = response.json()["message"]["content"]

        # Try to parse the model output as JSON and validate it
        import json
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            raise ValueError("Response content is not valid JSON.")

        return response_format.parse_obj(parsed)

    except (httpx.HTTPError, ValidationError, ValueError) as e:
        raise RuntimeError(f"Ollama structured call failed: {e}")
