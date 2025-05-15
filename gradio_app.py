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

def chat_interface(user_message: str) -> Generator[tuple[list[dict], str], None, None]:
    global chat_history
    from shared.state import stream_cache

    yield chat_history + [{"role": "assistant", "content": "Thinking..."}], ""

    user_msg = {"role": "user", "content": user_message}
    chat_history.append(user_msg)

    response_stream = root_workflow(messages=chat_history)

    full_response = ""
    thread_id = None
    last_state = None

    for partial in response_stream:
        if isinstance(partial, dict):
            last_state = next(reversed(partial.values()), {})
            thread_id = last_state.get("thread_id")
            result = last_state.get("result", "")
            full_response += result or ""
        else:
            full_response += str(partial)

        yield chat_history + [{"role": "assistant", "content": full_response.strip()}], ""

    if thread_id and thread_id in stream_cache:
        cached_stream = stream_cache.pop(thread_id)
        for item in cached_stream:
            try:
                if hasattr(item, "choices") and item.choices[0].delta:
                    content = item.choices[0].delta.content
                elif isinstance(item, str):
                    content = item
                else:
                    content = str(item)
            except Exception as e:
                print("⚠️ Error extracting content from stream:", e)
                continue

            if content:
                full_response += content
                yield chat_history + [{"role": "assistant", "content": full_response.strip()}], ""

    if full_response:
        chat_history.append({"role": "assistant", "content": full_response.strip()})
    else:
        message = None
        if last_state and isinstance(last_state, dict):
            messages = last_state.get("messages", [])
            for msg in reversed(messages):
                if "content" in msg:
                    message = msg["content"]
                    break
        chat_history.append({
            "role": "assistant",
            "content": message.strip() if message else "[no response]"
        })

    yield chat_history, ""


with gr.Blocks(css="""
#chat-container {
    display: flex;
    flex-direction: column;
    height: 90vh;
    padding: 0;
    margin: 0;
}

#chatbot {
    flex-grow: 1;
    overflow: auto;
    border: 1px solid #444;
    margin-bottom: 0.5rem;
}

#msg-box {
    margin: 0;
}
""") as demo:
    with gr.Column(elem_id="chat-container"):
        gr.Markdown("### Chat with the Hive Mind")
        chatbot = gr.Chatbot(label="I have spoken", type="messages", elem_id="chatbot")
        msg = gr.Textbox(label="You may grovel here:", elem_id="msg-box")

        msg.submit(fn=chat_interface, inputs=msg, outputs=[chatbot, msg])

        clear_btn = gr.Button("🧹 Clear Chat")

        def clear_chat():
            global chat_history
            chat_history.clear()
            return [], ""  # Clear chatbot messages and input box

        clear_btn.click(fn=clear_chat, outputs=[chatbot, msg])

if __name__ == "__main__":
    demo.queue().launch(server_name="0.0.0.0", server_port=7977)
