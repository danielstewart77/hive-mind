import json
import os
from openai import OpenAI
from dotenv import load_dotenv

from services.openai_tool_wrapper import get_tools

load_dotenv(dotenv_path='secrets.env')


def call_tools_openai(messages: list[dict[str, str]]) -> dict:

    messages.append({
        "role": "system",
        "content": f'''Choose one or multiple tools that would be useful for solving the task.'''
    })

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
                "role": "function",
                "tool_call_id": tool_call.id,
                "name": name,
                "content": result
            })
        
    completion = client.chat.completions.create(
        model="gpt-4o",
        messages=messages
    )

    content = completion.choices[0].message.content
    return content