import logging
import os
import sys
from typing import Generator
from agent_tooling import OpenAITooling, discover_tools
from flask import Flask, request, jsonify, Response, stream_with_context
import json

from utilities import tool_discovery

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)
app = Flask(__name__)

@app.route("/chat", methods=["POST"])
def chat():
    try:
        data = request.get_json()
        chat_content = data.get("messages", [])[-1].get("content", "").strip()
        messages = data.get("messages", [])
        
        if not chat_content:
            return jsonify({"error": "No messages found."}), 400
        
        OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
        openai = OpenAITooling(api_key=OPENAI_API_KEY, model="gpt-4o")
        
        # Define the generator function with the captured variables
        @stream_with_context
        def generate_response():
            discover_tools(['agents', 'utilities'])
            #tool_discovery.discover_tools()
            result = openai.call_tools(messages=messages)
            
            if isinstance(result, Generator):
                for partial in result:
                    app.logger.debug(f"Streaming partial: {partial}")
                    
                    if isinstance(partial, dict):
                        yield f"data: {json.dumps(partial)}\n\n"
                    elif isinstance(partial, str):
                        if partial.startswith('{') and partial.endswith('}'):
                            yield f"data: {partial}\n\n"
                        else:
                            yield f"data: {json.dumps({'response': partial})}\n\n"
                    else:
                        yield f"data: {json.dumps(partial)}\n\n"
            else:
                # For non-generator results
                yield f"data: {json.dumps({'response': result})}\n\n"
        
        # Return the streaming response
        return Response(generate_response(), content_type="text/event-stream")
        
    except Exception as e:
        app.logger.error(f"Error in chat endpoint: {str(e)}")
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500

@app.route("/agent_code", methods=["GET"])
def get_agent_code():
    """Returns agent_0s' code"""
    try:
        with open("agent_0.py", "r") as file:
            file_content = file.read()
            return f"Agent 0 🔫😎: {file_content}"
    except Exception as e:
        return jsonify({"error": f"Could not read agent code: {str(e)}"}), 500

def main():
    """Main function to start the Flask app"""
    app.run(host="0.0.0.0", port=7977, debug=True)

if __name__ == "__main__":
    main()