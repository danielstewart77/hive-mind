import os
import json
import gradio as gr
from typing import Generator
from agent_tooling import OpenAITooling, discover_tools
from workflows.root import root_workflow

# Initialize tooling
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai = OpenAITooling(api_key=OPENAI_API_KEY, model="gpt-4o")

discover_tools()

# This will keep track of the entire chat history
chat_history = []

chatbot = gr.Chatbot(label="Chat", type="messages")

import gradio as gr
from typing import Generator

# Global chat history in OpenAI format
chat_history = []

def chat_interface(user_message: str) -> Generator[list[dict], None, None]:
    global chat_history

    user_msg = {"role": "user", "content": user_message}

    # Append the user message to the chat history
    chat_history.append(user_msg)

    assistant_response = root_workflow(
        messages=chat_history
    )
    
    full_response = ""

    for i, word in enumerate(assistant_response.split()):
        full_response += word + " "
        yield chat_history + [user_msg, {"role": "assistant", "content": full_response.strip()}]

    chat_history.extend([user_msg, {"role": "assistant", "content": full_response.strip()}])

with gr.Blocks() as demo:
    gr.Markdown("### Chat with Agent")
    chatbot = gr.Chatbot(label="Chat", type="messages")
    msg = gr.Textbox(label="Your message")

    msg.submit(fn=chat_interface, inputs=msg, outputs=chatbot)

if __name__ == "__main__":
    demo.queue().launch(server_name="0.0.0.0", server_port=7977)
