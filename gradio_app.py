import os
import json
import gradio as gr
from typing import Generator
from agent_tooling import OpenAITooling, discover_tools
from workflows.root import root_workflow
global chat_history
from shared.state import stream_cache
import shared.state as global_state
from langgraph.types import Command
from utilities.messages import get_last_user_message

# Initialize tooling
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
agent_tooling_openai = OpenAITooling(api_key=OPENAI_API_KEY, model="gpt-4o")

discover_tools(['agents', 'workflows', 'utilities'])

# This will keep track of the entire chat history
chat_history = []

def chat_interface(user_message: str, tags_csv: str) -> Generator[tuple[list[dict], str], None, None]:

    tags = [t.strip() for t in tags_csv.split(",") if t.strip()]
    yield chat_history + [{"role": "assistant", "content": "Thinking..."}], ""

    user_msg = {"role": "user", "content": user_message}
    chat_history.append(user_msg)

    if global_state.workflow_id and global_state.workflow_id in global_state.workflows:
        workflow = global_state.workflows[global_state.workflow_id]["workflow"]
        thread_id = global_state.workflows[global_state.workflow_id]["thread_id"]

        last_user_message = get_last_user_message(chat_history)

        response_stream = workflow.stream(
            Command(resume=last_user_message),
            config={"configurable": {"thread_id": thread_id}}
        )

        full_response = ""
        thread_id = None
        last_state = None

        for partial in response_stream:
            if isinstance(partial, dict):
                last_state = next(reversed(partial.values()), {})
                # thread_id = last_state.get("thread_id")
                result = last_state.get("message", "")
                full_response += result or ""

            else:
                full_response += str(partial)

            yield chat_history + [{"role": "assistant", "content": full_response.strip()}], ""

    else:

        response_stream = agent_tooling_openai.call_tools(
            messages=chat_history,
            model="gpt-4.1",
            tool_choice="auto",
            tags=tags,
            fallback_tool="web_search",
        )

    # result_stream = ollama_tooling_client.call_tools(
    #     messages=messages,
    #     model="granite3.3:2b",
    #     tool_choice="auto",
    #     tags=tags,
    #     fallback_tool="web_search",
    # )

    full_response = ""

    for partial in response_stream:

        full_response += str(partial)

        yield chat_history + [{"role": "assistant", "content": full_response.strip()}], ""

    if full_response:
        chat_history.append({"role": "assistant", "content": full_response.strip()})

with gr.Blocks(css="""
#chat-container {
    display: flex;
    flex-direction: column;
    height: 90%;
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

        with gr.Row():
            chatbot = gr.Chatbot(label="I have spoken", type="messages", elem_id="chatbot", scale=9)
            tags_input = gr.Textbox(label="Tool Tags", placeholder="e.g. workflows, utilities", scale=1)


        msg = gr.Textbox(label="You may grovel here:", elem_id="msg-box")

        msg.submit(fn=chat_interface, inputs=[msg, tags_input], outputs=[chatbot, msg])

        clear_btn = gr.Button("🧹 Clear Chat")

        def clear_chat():
            global chat_history
            chat_history.clear()
            return [], ""  # Clear chatbot messages and input box

        clear_btn.click(fn=clear_chat, outputs=[chatbot, msg])

if __name__ == "__main__":
    demo.queue().launch(server_name="0.0.0.0", server_port=7977)
