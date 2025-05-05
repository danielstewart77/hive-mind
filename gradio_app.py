import os
import json
import gradio as gr
from typing import Generator
from agent_tooling import OpenAITooling, discover_tools
from workflows.root import root_workflow

# Initialize tooling
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai = OpenAITooling(api_key=OPENAI_API_KEY, model="gpt-4o")

discover_tools(['agents', 'workflows', 'utilities'])

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
    chat_history.append(user_msg)

    assistant_response = root_workflow(messages=chat_history)

    # Case 1: Streaming generator
    if isinstance(assistant_response, Generator):
        full_response = ""
        for partial in assistant_response:
            chunk = (
                partial.get("response")
                if isinstance(partial, dict) and "response" in partial
                else str(partial)
            )
            full_response += chunk
            yield chat_history + [{"role": "assistant", "content": full_response.strip()}]
        chat_history.append({"role": "assistant", "content": full_response.strip()})

    # Case 2: Regular string
    else:
        full_response = str(assistant_response)
        chat_history.append({"role": "assistant", "content": full_response})
        yield chat_history


with gr.Blocks() as demo:
    gr.Markdown("### Chat with Agent")
    chatbot = gr.Chatbot(label="Chat", type="messages")
    msg = gr.Textbox(label="Your message")

    msg.submit(fn=chat_interface, inputs=msg, outputs=chatbot)

if __name__ == "__main__":
    demo.queue().launch(server_name="0.0.0.0", server_port=7977)
