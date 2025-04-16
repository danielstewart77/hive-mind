import logging
import os
import traceback
from typing import Dict, Any, Generator, Literal, TypedDict,Optional
from langgraph.graph import StateGraph, END, START
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, Field
import uuid

from agents.maker import create_local_repository, generate_code, generate_requirements, suggest_agent_name, update_requirements
from agent_tooling import tool

from agents.openai import completions_structured

# Define our state structure
class State(TypedDict):
    code_instructions: str
    generated_code: str
    required_libraries: str
    message: str
    step: str
    user_feedback: str
    suggested_name: str
    agent_name: str

class UserFeedback(BaseModel):
    approve: bool = Field(..., description="""This value is derived from the user's feedback.""")
    user_feedback: str = Field(..., description="""The user's feedback on the code. This value MUST 
                               come VERBATIM from the user's message. DO NOT change this in ANY way. """)

    class Config:
        extra = "forbid"

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
    
    generated_code = generate_code(
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
        question = f"Do you approve this code:\n```python \r{state["generated_code"]}"
        feedback = interrupt(
            {
                "question": question
            }
        )
    elif state["step"] == "libraries":
        question = f"Do you approve these additional libraries:\n ```\r{'\n'.join(state['required_libraries']['required_libraries'])}"
        feedback = interrupt(
            {
                "question": question
            }
        )
    elif state["step"] == "name":
        question = f"Can we call your agent {state['suggested_name']}?"
        feedback = interrupt(
            {
                "question": question
            }
        )

    else:
        raise ValueError("Invalid step for user feedback.")

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
    user_feedback = state["user_feedback"]
    suggested_name = suggest_agent_name(agent_code=generated_code, user_feedback=user_feedback)

    # Update the state with the generated name
    new_state = state.copy()
    new_state["suggested_name"] = suggested_name

    return new_state

def write_required_libraries(state: State) -> State:
    # update the requirements.txt file with the required libraries
    required_libraries = state["required_libraries"]
    if required_libraries:
        # Call the function to update requirements
        update_requirements(required_libraries=required_libraries)

def write_code(state: State) -> State:
    """Save the final approved code"""
    # Define the folder path relative to the script location
    generated_code = state["generated_code"]
    create_local_repository(agent_code=generated_code, agent_name="temp_value")

    agent_name = "temp_value"
    folder = os.path.dirname(__file__)
    file = os.path.join(folder, f"{agent_name}.py")

    # Ensure the directory exists
    if not os.path.exists(folder):
        os.makedirs(folder)
        logging.info(f"Created directory: {folder}")

    # Write the generated code to the main file
    with open(file, "w") as f:
        logging.info(f"Writing code to file: {file}")
        f.write(state["generated_code"])
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
    
    # Add the START edge - this was missing in the original code
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
        elif approve is True and step == "libraries":
            return "write_required_libraries"
        elif approve is False and step == "libraries":
            return "generate_code"
        elif approve is True and step == "name":
            return "write_required_libraries"
        elif approve is False and step == "name":
            return "generate_agent_name"
        else:
            raise ValueError(f"Unhandled state: approve={approve}, step={step}")

    # Add the conditional edges
    graph.add_conditional_edges(
        "get_user_feedback",
        conditional_transition
    )
    
    graph.add_edge("write_required_libraries", "write_code")
    graph.add_edge("write_code", END)
    
    # Add a checkpointer when compiling
    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)

""""""

# Global storage for in-progress workflows
active_workflows = {}

@tool
def create_agent(
    code_instructions: str, 
    workflow_id: Optional[str] = None,
    user_feedback: Optional[UserFeedback] = None
) -> Generator[str, None, None]:
    """
    This function handles agent creation based on user requirements.
    DO NOT assign a value to the workflow_id parameter, unless it is given in the message.
    DO NOT assign a value to the user_feedback parameter, unless it is given in the message.

    Parameters:
    - code_instructions (str): The user ccode instructions for the agent.
    - workflow_id (str, optional): The ID of the workflow to resume.
    - user_feedback ({"approve": bool, "user_feedback": str | None}}, optional): The last message from the user that answers the question: Do you approve... .
    """
    try:
        # Define stream variable outside the if/else blocks
        stream = None
        
        # If we have a workflow_id, we're resuming an existing workflow
        if workflow_id and workflow_id in active_workflows:
            workflow = active_workflows[workflow_id]["workflow"]
            thread_id = active_workflows[workflow_id]["thread_id"]
            
            # Use stream instead of invoke for consistency
            stream = workflow.stream(
                Command(resume=user_feedback),
                config={"configurable": {"thread_id": thread_id}}
            )
            
            # Stream progress messages
            #yield f"Continuing agent creation workflow...\n"
            
        else:
            # Start a new workflow
            workflow = create_agent_workflow()
            
            # Generate a new workflow ID if we don't have one
            workflow_id = str(uuid.uuid4())
                
            # Configure a thread ID for this workflow
            thread_id = workflow_id
            
            # Start the workflow with initial requirements
            #yield f"Starting agent creation with requirements: {agent_requirements}"
            
            # Stream the workflow with the initial state
            stream = workflow.stream(
                {
                    "code_instructions": code_instructions,
                    "generated_code": "",
                    "required_libraries": "",
                    "message": "",
                    "step": "",
                    "user_feedback": "",
                },
                config={"configurable": {"thread_id": thread_id}}
            )
            
            # Store the workflow and thread ID for later resumption
            active_workflows[workflow_id] = {
                "workflow": workflow,
                "thread_id": thread_id
            }
        
        # Process the stream
        found_interrupt = False
        final_state = None
        
        # Make sure stream exists before trying to iterate over it
        if stream:
            for chunk in stream:
                #yield f"DEBUG - Chunk: {chunk}"
                
                # Check if this chunk contains an interrupt
                if isinstance(chunk, dict) and "__interrupt__" in chunk:
                    found_interrupt = True
                    interrupt_info = chunk["__interrupt__"][0]
                    
                    # Extract the interrupt value
                    interrupt_value = interrupt_info.value if hasattr(interrupt_info, 'value') else interrupt_info
                    
                    # Extract the question and context
                    question = "User input required"
                    
                    if isinstance(interrupt_value, dict):
                        question = interrupt_value.get("question", question)
                    
                    # Format the response to indicate we need user input
                    yield f" workflow_id: {workflow_id}\n\n{question}"
                    break
                
                # Keep track of the final state
                final_state = chunk
        
        # If we didn't find an interrupt, the workflow completed
        if not found_interrupt:
            result_message = "Awesomesocks, you're agent is completed!"
            
            if final_state and isinstance(final_state, dict):
                if "message" in final_state:
                    result_message = final_state["message"]
                elif isinstance(final_state.get("write_code"), dict) and "message" in final_state["write_code"]:
                    result_message = final_state["write_code"]["message"]
            
            # Clean up the stored workflow
            if workflow_id in active_workflows:
                del active_workflows[workflow_id]
                
            yield result_message
            
    except Exception as e:
        yield f"Error in create_agent: {str(e)}\nTraceback: {traceback.format_exc()}"