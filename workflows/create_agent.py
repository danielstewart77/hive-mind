import logging
import os
import traceback
from typing import Dict, Any, Generator, Literal, TypedDict,Optional
from langgraph.graph import StateGraph, END, START
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, Field
import uuid

from agents.maker import create_local_repository, generate_agent_code, generate_requirements, extract_function_name, update_requirements
from agent_tooling import tool

from utilities.openai_tools import completions_structured
from utilities.messages import get_last_user_message
from workflows.models.feedback import UserFeedback
import shared.state as global_state

# Define our state structure
class State(TypedDict):
    code_instructions: str
    generated_code: str
    required_libraries: str
    message: str
    step: str
    user_feedback: str
    agent_name: str

# Create nodes for our workflow
def generate_code(state: State) -> State:
    """Generate initial agent code based on requirements"""

    current_step = state["step"]
    if current_step == "libraries" or current_step == "code":
        code_instructions = state["code_instructions"]
        current_code = state["generated_code"]
        # call llm to reword requirements with updated requirements
        user_feedback = state["user_feedback"]

        code_instructions = f"update the code: {current_code} \n to meet these updated insructions: {user_feedback}"
    else:
        code_instructions = state["code_instructions"]
    
    generated_code = generate_agent_code(
        agent_requirements=code_instructions,
        llm_provider="openai")
    
    new_state = state.copy()
    new_state["generated_code"] = generated_code
    new_state["step"] = "code"

    return new_state

def get_user_feedback(state: State) -> State:
    """Get human feedback on the generated code"""
    # Interrupt the workflow to get human feedback

    if state["step"] == "code":
        question = f"```python \r{state['generated_code']}```\n\nDo you approve this code?"
        feedback = interrupt(
            {
                "question": question
            }
        )
    elif state["step"] == "libraries" and state["required_libraries"] is not None:
        libs = '\n'.join(state['required_libraries'])
        question = f"Do you approve these additional libraries:\n```\n{libs}\n```"
        feedback = interrupt({
            "question": question
        })

    elif state["step"] == "libraries" and state["required_libraries"] is None:
        updated_state = state.copy()
        updated_state["approve"] = True
        return updated_state

    else:
        raise ValueError(f"Invalid step for user feedback => state:{state['step']} feedback:{feedback}")

    message = f"""Read the user's feedback and determine if the code is approved.
     If the code is not approved, extract EXACT WORDS VERBATIM from user's message for updated code requirements.
     feedback: {feedback}"""

    user_feedback = completions_structured(message=message, response_format=UserFeedback)
    
    # Store the approval status and updated requirements in the state
    updated_state = state.copy()
    updated_state["approve"] = user_feedback.approve
    if state["step"] == "name":
        updated_state["agent_name"] = state["suggested_name"]
    
    if not user_feedback.approve:
        # If not approved, update the requirements
        if state["step"] == "name":
            updated_state["user_feedback"] += " " + user_feedback.user_feedback
        updated_state["user_feedback"] = user_feedback.user_feedback
    
    # Return the updated state - the router will handle the branching
    return updated_state

def generate_required_libraries(state: State) -> State:
    """Generate requirements based on the approved code"""
    agent_code = state["generated_code"]

    libraries_update = generate_requirements(agent_code=agent_code)

    new_state = state.copy()
    new_state["step"] = "libraries"
    new_state["required_libraries"] = libraries_update

    return new_state

def generate_agent_name(state: State) -> State:
    """Generate a name for the agent"""
    # Call the function to generate a name
    generated_code = state["generated_code"]
    name = extract_function_name(agent_code=generated_code)

    # Update the state with the generated name
    new_state = state.copy()
    new_state["agent_name"] = name
    new_state["step"] = "name"

    return new_state

def write_required_libraries(state: State) -> State:
    # update the requirements.txt file with the required libraries
    required_libraries = state["required_libraries"]
    if required_libraries:
        # Call the function to update requirements
        update_requirements(required_libraries=required_libraries)

    return state

def write_code(state: State) -> State:
    """Save the final approved code"""
    # Define the folder path relative to the script location
    generated_code = state["generated_code"]
    agent_name = state["agent_name"]
    create_local_repository(agent_code=generated_code, agent_name=agent_name)

    del global_state.workflows[global_state.workflow_id]
    global_state.workflow_id = None

    return {
        "code_instructions": state["code_instructions"],
        "generated_code": state["generated_code"],
        "required_libraries": state["required_libraries"],
        "message": "Agent has been successfully created!",
        "step": "done"
    }

# Create the workflow graph
def create_agent_workflow():
    # Initialize the graph
    graph = StateGraph(State)
    
    # Add nodes
    graph.add_node("generate_code", generate_code)
    graph.add_node("get_user_feedback", get_user_feedback)
    graph.add_node("generate_required_libraries", generate_required_libraries)
    graph.add_node("generate_agent_name", generate_agent_name)
    graph.add_node("write_required_libraries", write_required_libraries)
    graph.add_node("write_code", write_code)
    
    # Add the edges
    graph.add_edge(START, "generate_code")
    graph.add_edge("generate_code", "get_user_feedback")
    graph.add_edge("generate_required_libraries", "get_user_feedback")
    
    # Define the conditional edge function
    def conditional_transition(state):
        approve = state.get("approve")
        step = state.get("step")

        if approve is True and step == "code":
            return "generate_required_libraries"
        elif approve is False and step == "code":
            return "generate_code"
        elif approve is True and step == "libraries" and state["required_libraries"] is not None:
            return "write_required_libraries"
        elif approve is True and step == "libraries" and state["required_libraries"] is None:
            return "generate_agent_name"
        elif approve is False and step == "libraries":
            return "generate_code"
        else:
            raise ValueError(f"Unhandled state: approve={approve}, step={step}")

    # Add the conditional edges
    graph.add_conditional_edges(
        "get_user_feedback",
        conditional_transition
    )
    
    graph.add_edge("generate_agent_name", "write_code")
    graph.add_edge("write_required_libraries", "write_code")
    graph.add_edge("write_code", END)

    # output_path = "workflows/diagrams/create_agent_workflow.png"
    # # Create the output directory if it doesn't exist
    # os.makedirs(os.path.dirname(output_path), exist_ok=True)
    # # Draw the graph
    # diagram = graph.get_graphviz()
    # diagram.draw(output_path, prog="dot", format="png")
    
    # Add a checkpointer when compiling
    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)


@tool(tags=["agent"])
def workflow_create_agent(
    messages: Optional[list[str]] = None,
) -> Generator[str, None, None]:
    """
    Resume the agent creation workflow using the workflow_id set by the root.
    Assumes that the workflow has already been initialized and is stored in shared.active_workflows.
    """
    try:
        # get the last user message
        last_user_message = get_last_user_message(messages)
        if not last_user_message:
            yield "No user message found. Please provide a message."
            return

        # Define stream variable outside the if/else blocks
        stream = None
            
        # Start a new workflow
        workflow = create_agent_workflow()
        
        # Generate a new workflow ID if we don't have one
        workflow_id = str(uuid.uuid4())
            
        # Configure a thread ID for this workflow
        thread_id = workflow_id
        
        # Stream the workflow with the initial state
        stream = workflow.stream(
            {
                "code_instructions": last_user_message,
                "generated_code": "",
                "required_libraries": "",
                "message": "",
                "step": "",
                "user_feedback": "",
            },
            config={"configurable": {"thread_id": thread_id}}
        )
        
        # Store the workflow and thread ID for later resumption
        global_state.workflow_id = workflow_id
        global_state.workflows[workflow_id] = {
            "workflow": workflow,
            "thread_id": thread_id
        }
        
        # Process the stream
        found_interrupt = False
        final_state = None

        for chunk in stream:
            if isinstance(chunk, dict) and "__interrupt__" in chunk:
                found_interrupt = True
                interrupt_info = chunk["__interrupt__"][0]
                interrupt_value = getattr(interrupt_info, "value", interrupt_info)

                question = "User input required"
                if isinstance(interrupt_value, dict):
                    question = interrupt_value.get("question", question)

                yield question
                break

            final_state = chunk

        if not found_interrupt:
            result_message = "✅ Awesomesocks, your agent is completed!"
            if isinstance(final_state, dict) and "message" in final_state:
                result_message = final_state["message"]

            # ✅ Clean up
            del global_state.workflows[global_state.workflow_id]
            from shared import state as shared_state
            shared_state.workflow_id = None

            yield result_message

    except Exception as e:
        yield f"❌ Error in workflow_create_agent: {str(e)}\nTraceback:\n{traceback.format_exc()}"