import os
import json
import gradio as gr
from typing import Generator
from agent_tooling import OpenAITooling
from utilities import tool_discovery

# Initialize tooling
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai = OpenAITooling(api_key=OPENAI_API_KEY, model="gpt-4o")

# This will keep track of the entire chat history
chat_history = []

chatbot = gr.Chatbot(label="Chat", type="messages")

def chat_interface(user_message: str):
    global chat_history
    messages = chat_history + [{"role": "user", "content": user_message}]
    
    tool_discovery.discover_tools()

    result = openai.call_tools(
        messages=messages,
        model="gpt-4.1",
        tags=["root_workflow"],
        #stream=True to stream responses, use stream_tools
    )

    if isinstance(result, Generator):
        full_response = ""
        for partial in result:
            if isinstance(partial, dict):
                chunk = partial.get("response", json.dumps(partial))
            else:
                chunk = str(partial)
            full_response += chunk
            yield chat_history + [{"role": "assistant", "content": full_response}]
        chat_history.append({"role": "user", "content": user_message})
        chat_history.append({"role": "assistant", "content": full_response})
    else:
        response = str(result)
        chat_history.append({"role": "user", "content": user_message})
        chat_history.append({"role": "assistant", "content": response})
        yield chat_history

with gr.Blocks() as demo:
    gr.Markdown("# 🤖 Chat with Agent")
    chatbot = gr.Chatbot()
    msg = gr.Textbox(label="Your message", placeholder="Ask me anything...")
    
    def user_submit(message):
        return chat_interface(message)

    msg.submit(fn=user_submit, inputs=msg, outputs=chatbot)

if __name__ == "__main__":
    demo.queue().launch(server_name="0.0.0.0", server_port=7977)
