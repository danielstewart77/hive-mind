import os
import json
import logging
import logging
import sys
from agent_tooling import tool, get_registered_tools
from typing import List, Dict, Tuple, Any
from flask import Flask, request, jsonify
from openai import OpenAI
from dotenv import load_dotenv
from agents.openai import add_numbers, message_autocomplete, create_title_and_emoji, create_tags, answer_question
from agents.coingeko import current_crypto_price
load_dotenv(dotenv_path='secrets.env')


# Configure logging to output to the console
logging.basicConfig(
    level=logging.INFO,  # Set the logging level
    format='%(asctime)s - %(levelname)s - %(message)s',  # Log format
    stream=sys.stdout  # Log to stdout (which Docker logs to by default)
)

# Create a logger instance
logger = logging.getLogger(__name__)

def get_tools() -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    functions = get_registered_tools()

    tools = []
    available_functions = {}

    for function in functions:
        tools.append({
            "type": "function",
            "function": {
                "name": function["name"],
                "description": function["description"],
                "parameters": function["parameters"],
                "return_type": function["return_type"],
            },
        })
        
        func_name = function["name"]
        available_functions[func_name] = globals().get(func_name)

    return tools, available_functions

def interpret_query(message: str) -> dict:
    """Interprets a user query and returns a standardized response dict."""
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

    tools, available_functions = get_tools()
    messages = [{"role": "user", "content": message}]

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
            return function_to_call(**args)

    # Direct LLM response with no tool call
    return {"response": completion.choices[0].message.content}

app = Flask(__name__)

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    chat_content = data.get("messages", [])[-1].get("content", "").strip()

    if not chat_content:
        return jsonify({"error": "No chat content found."}), 400
    
    try:
        # Get the response from the interpretation function
        result = interpret_query(chat_content)
        
        # Return the result directly (tools now return standardized format)
        return jsonify({"response": result})
    except Exception as e:
        app.logger.error(f"Error in chat endpoint: {str(e)}")
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500

# an endpoint to return this file in text format
@tool
def get_agent_code() -> str:
    """Returns the agent_0s' code"""
    with open("agent_0.py", "r") as file:
        file = file.read()
        return jsonify({"response": file})

def main():
    """Main function to start the Flask app"""
    app.run(host="0.0.0.0", port=7977, debug=True)

if __name__ == "__main__":
    main()
