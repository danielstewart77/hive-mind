
import os
import traceback
from typing import Generator, Optional, TypedDict

#from gradio import List
from dotenv import load_dotenv
from langgraph.graph import StateGraph, END, START
from langgraph.types import Command
from langgraph.checkpoint.memory import MemorySaver
from pydantic import Field
import uuid
from utilities.messages import get_last_user_message
from agent_tooling import OpenAITooling, OllamaTooling
from utilities.openai_tools import completions_structured
from pydantic import BaseModel

load_dotenv()

OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

from shared.state import stream_cache

ollama_tooling_client = OllamaTooling(model="llama3.2:8b")
agent_tooling_openai = OpenAITooling(api_key=OPENAI_API_KEY)

class TriageRequestType(BaseModel):
    agent: bool = Field(description="Whether the request is for an agent")

    class Config:
        extra = "forbid"

# Define our state structure
class State(TypedDict):
    user_feedback: str
    messages: Optional[list[dict[str, str]]] = Field(default_factory=list)
    result: Optional[str] = None
    stream: Optional[Generator] = None
    thread_id: Optional[str] = None
    tags: Optional[list[str]] = Field(default_factory=list)

def triage_temp(messages: Optional[list[dict[str, str]]] = None) -> Generator[str | dict, None, None]:
    """call this function any time the user specifically mentions agents or tools"""
    yield "🛠️  Triaging request...\n\n\n"

    if not messages:
        yield "No messages provided. Please provide a message."

    message = get_last_user_message(messages)
    if not message:
        yield "No user message found. Please provide a message."
        return

    agent = completions_structured(
        message=message,
        response_format=TriageRequestType,
        model="gpt-4.1",
    ).agent

    if agent:
        yield from ollama_tooling_client.call_tools(
                messages=messages,
                model="qwen3:8b",
                tool_choice="auto",
                tags=["agent"]
            )
    else:
        yield from ollama_tooling_client.call_tools(
                messages=messages,
                model="qwen3:8b",
                tool_choice="auto",
                tags=["triage"],
                fallback_tool="web_search",
            )


def triage(state: State) -> State:
    try:
        print("🧪 Entered triage node with state:", state)
        
        messages = state["messages"]
        tags = state.get("tags", [])

        result_stream = agent_tooling_openai.call_tools(
            messages=messages,
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


        #result_stream = triage_temp(messages)

        # Store it globally using thread ID or workflow ID
        thread_id = state.get("thread_id")  # make sure thread_id is included in state
        print("🧵 thread_id in triage:", thread_id)
        if thread_id:
            stream_cache[thread_id] = result_stream
        else:
            print("❌ No thread_id found in state. Stream will be lost.")

        return state

    except Exception as e:
        new_state = state.copy()
        new_state["result"] = f"Error in triage: {str(e)}"
        return new_state



# Create the workflow graph
def create_root_workflow():
    # Initialize the graph
    graph = StateGraph(State)
    
    # Add nodes
    graph.add_node("triage", triage)
    
    # Add the edges
    graph.add_edge(START, "triage")
    
    graph.add_edge("triage", END)
    
    # Add a checkpointer when compiling
    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)

# Global storage for in-progress workflows
active_workflows = {}

def root_workflow(
    workflow_id: Optional[str] = None,
    messages: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
) -> Generator[str, None, None]:
    try:
        last_user_message = get_last_user_message(messages)
        if not last_user_message:
            yield "No user message found. Please provide a message."
            return

        if workflow_id and workflow_id in active_workflows:
            workflow = active_workflows[workflow_id]["workflow"]
            thread_id = active_workflows[workflow_id]["thread_id"]

            yield from workflow.stream(
                Command(resume=last_user_message),
                config={"configurable": {"thread_id": thread_id}}
            )
        else:
            workflow = create_root_workflow()
            workflow_id = str(uuid.uuid4())
            thread_id = workflow_id

            stream = workflow.stream(
                {
                    "messages": messages,
                    "user_feedback": "",
                    "result": "",
                    "thread_id": thread_id,
                    "tags": tags or [],
                },
                config={"configurable": {"thread_id": thread_id}}
            )

            active_workflows[workflow_id] = {
                "workflow": workflow,
                "thread_id": thread_id
            }

            yield from stream

    except Exception as e:
        def error_stream():
            yield f"Error in create_agent: {str(e)}\nTraceback: {traceback.format_exc()}"
        yield from error_stream()

