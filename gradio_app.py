import os
import json
import gradio as gr
from typing import Generator
from agent_tooling import OpenAITooling, discover_tools
from workflows.root import root_workflow

from shared.state import stream_cache


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
    from shared.state import stream_cache

    yield chat_history + [{"role": "assistant", "content": "Thinking..."}]


    user_msg = {"role": "user", "content": user_message}
    chat_history.append(user_msg)

    response_stream = root_workflow(messages=chat_history)

    full_response = ""
    thread_id = None

    for partial in response_stream:
        if isinstance(partial, dict):
            last_state = next(reversed(partial.values()), {})
            thread_id = last_state.get("thread_id")
            result = last_state.get("result", "")
            full_response += result
            yield chat_history + [{"role": "assistant", "content": full_response.strip()}]
        else:
            full_response += str(partial)
            yield chat_history + [{"role": "assistant", "content": full_response.strip()}]

    print("📦 stream_cache keys:", list(stream_cache.keys()))

    if thread_id and thread_id in stream_cache:
        stream = stream_cache.pop(thread_id)
        for item in stream:
            try:
                content = item.choices[0].delta.content
            except Exception as e:
                print("⚠️ Failed to extract content:", e)
                continue

            if content:
                full_response += content
                yield chat_history + [{"role": "assistant", "content": full_response.strip()}]


    chat_history.append({"role": "assistant", "content": full_response.strip()})




with gr.Blocks() as demo:
    gr.Markdown("### Chat with the Hive Mind")
    chatbot = gr.Chatbot(label="Chat", type="messages")
    msg = gr.Textbox(label="Your message")

    msg.submit(fn=chat_interface, inputs=msg, outputs=chatbot)

if __name__ == "__main__":
    demo.queue().launch(server_name="0.0.0.0", server_port=7977)
