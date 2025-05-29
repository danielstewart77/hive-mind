import os
import gradio as gr
from typing import Generator
from agent_tooling import OpenAITooling, discover_tools, OllamaTooling
global chat_history
import shared.state as global_state
from langgraph.types import Command
from utilities.messages import get_last_function_message, get_last_user_message, mentions_editor
from gradio.routes import mount_gradio_app
import uvicorn

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# Initialize tooling
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
agent_tooling_openai = OpenAITooling(api_key=OPENAI_API_KEY, model="gpt-4o")
ollama_tooling_client = OllamaTooling()

discover_tools(['agents', 'workflows', 'utilities'])

# This will keep track of the entire chat history
messages = []

def chat_interface(
    user_message: str,
    tags_csv: str,
    model_source: str,
    internal_model: str,
    provider: str,
    openai_model: str,
    anthropic_model: str
) -> Generator[tuple[list[dict], str, gr.HTML], None, None]:
    tags = [t.strip() for t in tags_csv.split(",") if t.strip()]
    yield messages + [{"role": "assistant", "content": "Thinking..."}], "", gr.update(visible=False)

    user_msg = {"role": "user", "content": user_message}
    messages.append(user_msg)

    if global_state.workflow_id and global_state.workflow_id in global_state.workflows:
        workflow = global_state.workflows[global_state.workflow_id]["workflow"]
        thread_id = global_state.workflows[global_state.workflow_id]["thread_id"]

        last_user_message = get_last_user_message(messages)

        response_stream = workflow.stream(
            Command(resume=last_user_message),
            config={"configurable": {"thread_id": thread_id}}
        )

        full_response = ""
        last_state = None

        for partial in response_stream:
            if isinstance(partial, dict):
                last_state = next(reversed(partial.values()), {})
                result = last_state.get("message", "")
                full_response += result or ""
            else:
                full_response += str(partial)

            yield messages + [{"role": "assistant", "content": full_response.strip()}], "", gr.update(visible=False)

    else:
        if model_source == "internal":
            response_stream = ollama_tooling_client.call_tools(
            messages=messages,
            model=internal_model,
            tool_choice="auto",
            tags=tags,
            fallback_tool="web_search",
        )
        else:
            if provider == "OpenAI":
                response_stream = agent_tooling_openai.call_tools(
                    messages=messages,
                    model=openai_model,
                    tool_choice="auto",
                    tags=tags,
                    fallback_tool="web_search",
                )
            elif provider == "Anthropic":
                raise Exception("Anthropic provider not implemented yet for tool calling.")
        
        full_response = ""
        for partial in response_stream:
            full_response += str(partial)
            yield messages + [{"role": "assistant", "content": full_response.strip()}], "", gr.update(visible=False)

    if full_response:
        messages.append({"role": "assistant", "content": full_response.strip()})

    if mentions_editor(messages):
        yield messages, "", gr.update(value=get_last_function_message(messages).replace("@editor", "").strip(), visible=True)
    else:
        yield messages, "", gr.update(visible=False)


def toggle_sidebar(visible_state):
    return gr.update(visible=not visible_state), not visible_state

def update_tags(selected_tags):
    return ", ".join(selected_tags)

def clear_chat():
    global messages
    messages.clear()
    return [], ""

def handle_model_source_selection(source):
    if source == "internal":
        return (
            gr.update(choices=["granite3.3:2b", "qwen3:8b", "qwen3:14b", "llama3.2:3b"], value="granite3.3:2b", visible=True, interactive=True),
            gr.update(visible=False),
            gr.update(visible=False, interactive=False),
            gr.update(visible=False, interactive=False)
        )
    elif source == "external":
        return (
            gr.update(visible=False, interactive=False),     # internal_models
            gr.update(value="OpenAI", visible=True),         # external_providers
            gr.update(choices=["gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o"],
                      value="gpt-4.1", visible=True, interactive=True),  # openai_models
            gr.update(visible=False, interactive=False)      # anthropic_models
        )

def handle_provider_selection(provider):
    if provider == "OpenAI":
        return (
            gr.update(choices=["gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o"], value="gpt-4.1", visible=True, interactive=True),
            gr.update(visible=False, interactive=False)
        )
    elif provider == "Anthropic":
        return (
            gr.update(visible=False, interactive=False),
            gr.update(choices=["claude-3-7-sonnet", "claude-3-5-haiku"], value="claude-3-7-sonnet", visible=True, interactive=True)
        )

with gr.Blocks(fill_height=True, css="""
html, body, .gradio-container {
    height: 100vh;
    margin: 0;
    padding: 0;
    overflow: hidden;
}
#main-row {
    height: 100%;
    width: 100%;
    display: flex;
    flex-direction: row;
}
#chat-container {
    display: flex;
    flex-direction: column;
    flex-grow: 1;
    height: 100%;
    padding: 0.5rem;
    gap: 0.5rem;
    width: 100%;
}
#chatbot {
    flex-grow: 1;
    min-height: 0;
    overflow: auto;
    border: 1px solid #444;
}
#msg-box {
    margin: 0;
}
#toggle-btn {
    background-color: #333;
    border: none;
    color: white;
    font-size: 20px;
    cursor: pointer;
    padding: 0.5rem;
    width: 100%;
    height: 2rem;
}
""") as demo:

    sidebar_visible = gr.State(False)

    with gr.Row(elem_id="main-row"):
        sidebar = gr.Column(visible=False)
        with sidebar:
            tag_choices = ["triage", "agent", "editor"]
            tag_checkboxes = gr.CheckboxGroup(
                choices=tag_choices,
                value=["triage"],
                label="Tool Tags"
            )
            tags_input = gr.Textbox(visible=False)
            tag_checkboxes.change(fn=update_tags, inputs=tag_checkboxes, outputs=tags_input)

            # 1️⃣ Radio to choose between internal/external
            model_source = gr.Radio(
                choices=["internal", "external"],
                value="internal",
                label="Model Source"
            )

            # 2️⃣ Dropdown for internal models
            internal_models = gr.Dropdown(
                choices=["granite3.3:2b", "qwen3:8b", "qwen3:14b", "llama3.2:3b"],
                label="models",
                visible=True,
                interactive=True
            )

            # 3️⃣ Dropdown for external providers
            external_providers = gr.Dropdown(
                choices=["OpenAI", "Anthropic"],
                label="LLM Provider",
                visible=False
            )

            # 4️⃣ Dropdowns for specific external models
            openai_models = gr.Dropdown(
                choices=[],
                label="models",
                visible=False
            )

            anthropic_models = gr.Dropdown(
                choices=[],
                label="models",
                visible=False
            )

            model_source.change(
                fn=handle_model_source_selection,
                inputs=[model_source],
                outputs=[internal_models, external_providers, openai_models, anthropic_models]
            )

            external_providers.change(
                fn=handle_provider_selection,
                inputs=[external_providers],
                outputs=[openai_models, anthropic_models]
            )

        with gr.Column(elem_id="chat-container"):
            toggle_btn = gr.Button("☰   Settings", elem_id="toggle-btn")
            gr.Markdown("### Chat with the Hive Mind")

            chatbot = gr.Chatbot(label="I have spoken", type="messages", elem_id="chatbot")

            html_output = gr.HTML(visible=False)
            msg = gr.Textbox(label="You may grovel here:", elem_id="msg-box")

            # ✅ This now calls your real streaming function
            msg.submit(
                fn=chat_interface, 
                inputs=[
                    msg,
                    tags_input,
                    model_source,
                    internal_models,
                    external_providers,
                    openai_models,
                    anthropic_models
                ], 
                outputs=[
                    chatbot, 
                    msg, 
                    html_output
                ]
            )            

            clear_btn = gr.Button("🧹 Clear Chat")
            clear_btn.click(fn=clear_chat, outputs=[chatbot, msg])

    toggle_btn.click(fn=toggle_sidebar, inputs=[sidebar_visible], outputs=[sidebar, sidebar_visible])

if __name__ == "__main__":
    demo.queue().launch(server_name="0.0.0.0", server_port=7977)