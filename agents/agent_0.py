import logging
import logging
import sys
from agent_tooling import tool
from flask import Flask, request, jsonify

from agents.large_tasks import answer_is_complete, decompose_task
from services.openai_tool_calling import call_tools_openai
from services.ollama_tool_calling import call_tools_ollama

# Configure logging to output to the console
logging.basicConfig(
    level=logging.INFO,  # Set the logging level
    format='%(asctime)s - %(levelname)s - %(message)s',  # Log format
    stream=sys.stdout  # Log to stdout (which Docker logs to by default)
)

# Create a logger instance
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    chat_content = data.get("messages", [])[-1].get("content", "").strip()
    messages = data.get("messages", [])

    if not chat_content:
        return jsonify({"error": "No messages found."}), 400
    
    try:
        # decomposed_task = decompose_task(chat_content)
        # #add the instruction to the messages
        # messages.append({
        #     "role": "system",
        #     "content": decomposed_task
        # })

        # messages.append({
        #     "role": "system",
        #     "content": "EVERY step defined above IS ACCOMPLISHED before considering this task complete."
        # })

        complete = False

        while not complete:
            # Get the response from the interpretation function
            result = call_tools_openai(messages=messages)
            #result = call_tools_ollama(messages=messages)

            # check if the answer is complete
            complete = answer_is_complete(result)
            if complete:
                break
        
        # Return last n messages with answers
        return jsonify({
            "response": result
        })

    except Exception as e:
        app.logger.error(f"Error in chat endpoint: {str(e)}")
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500

# an endpoint to return this file in text format
def get_agent_code() -> str:
    """Returns agent_0s' code"""
    with open("agent_0.py", "r") as file:
        file = file.read()
        return f"Agent 0 🔫😎: {file}"

def main():
    """Main function to start the Flask app"""
    app.run(host="0.0.0.0", port=7977, debug=True)

if __name__ == "__main__":
    main()
