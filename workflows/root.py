import logging
import os
import traceback
from typing import Dict, Any, Generator, Literal, TypedDict,Optional
from langgraph.graph import StateGraph, END, START
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, Field
import uuid

from agent_tooling import OpenAITooling

from agents.maker import create_local_repository, generate_agent_code, generate_requirements, extract_function_name, update_requirements
from agent_tooling import tool

from utilities.openai_tools import completions_structured
from utilities.messages import get_last_message, get_last_user_message
from workflows.models.feedback import UserFeedback
from agents.websearch_openai import web_search

OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
        
from shared.state import stream_cache



# Define our state structure
class State(TypedDict):
    user_feedback: str
    messages: Optional[list[dict[str, str]]] = Field(default_factory=list)
    result: Optional[str] = None
    stream: Optional[Generator] = None
    thread_id: Optional[str] = None

def triage(state: State) -> State:
    try:
        print("🧪 Entered triage node with state:", state)
        openai = OpenAITooling(api_key=OPENAI_API_KEY, fallback_tool=web_search)
        messages = state["messages"]

        result_stream = openai.call_tools(
            messages=messages,
            model="gpt-4.1-mini",
            tags=["triage"],
            stream=True
        )

        # Store it globally using thread ID or workflow ID
        thread_id = state.get("thread_id")  # make sure thread_id is included in state
        print("🧵 thread_id in triage:", thread_id)
        if thread_id:
            stream_cache[thread_id] = result_stream
        else:
            print("❌ No thread_id found in state. Stream will be lost.")

        # new_state = state.copy()
        # new_state["result"] = "[streaming started]"
        # return new_state
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
                    "thread_id": thread_id,  # ✅ include this!
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

