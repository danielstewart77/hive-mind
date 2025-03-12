import json
import os
from openai import OpenAI
from dotenv import load_dotenv

from services.agent_tooling import get_tools

load_dotenv(dotenv_path='secrets.env')


def call_tools_openai(messages: list[dict[str, str]]) -> dict:
    """Interprets a user query and returns a standardized response dict."""
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

    tools, available_functions = get_tools()
    messages = messages

    completion = client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        tools=tools,
        tool_choice="auto",
    )
    response = completion.choices[0].message
    tool_calls = response.tool_calls
    
    if tool_calls:
        for tool_call in tool_calls:
            name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)
            function_to_call = available_functions[name]
            
            # Tool functions now return standardized responses
            result = function_to_call(**args)
            messages.append({
                "role": "system",
                "content": result
            })
            return messages

    # Direct LLM response with no tool call
    return {"response": completion.choices[0].message.content}