import traceback
from typing import Generator, Optional, TypedDict
import uuid
from langgraph.graph import StateGraph, END, START
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt, Command
from agent_tooling import tool, Agent

from agents.agent_editor import update_agent_code_workflow
from agents.maker import create_local_repository, generate_requirements, update_requirements
from agents.openai import completions_structured
from utilities.messages import get_last_user_message
from workflows.models.feedback import UserFeedback
from agents.agent_read import get_agent_by_name, get_agent_by_description

# Define our state structure
class State(TypedDict):
    code_instructions: str
    generated_code: str
    required_libraries: str
    message: str
    step: str
    user_feedback: str
    agent: Optional[Agent]

def find_agent(state: State) -> State:
    """Find the agent to be updated based on user input"""

    message = state["code_instructions"]
    
    agent = get_agent_by_name(name=message)
    if agent is None:
        # If no exact match, search by description
        agent = get_agent_by_description(description=message)
    if agent is None:
        raise ValueError(f"No agent found with name or description: {message}")
    # Store the agent in the state
    updated_state = state.copy()
    updated_state["step"] = "find_agent"
    updated_state["agent"] = agent

    return updated_state

def update_code(state: State) -> State:
    """Update the code of an agent based on user instructions"""
    # Get the agent name and update description from the state
    agent_name = state["agent"].name
    update_description = state["code_instructions"]

    # Call the function to update the agent code
    updated_code = update_agent_code_workflow(agent_name, update_description)

    # Store the updated code in the state
    updated_state = state.copy()
    updated_state["step"] = "update_code"
    updated_state["generated_code"] = updated_code

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

def write_required_libraries(state: State) -> State:
    # update the requirements.txt file with the required libraries
    required_libraries = state["required_libraries"]
    if required_libraries:
        # Call the function to update requirements
        update_requirements(required_libraries=required_libraries)
    updated_state = state.copy()
    updated_state["step"] = "libraries"
    return updated_state

def write_code(state: State) -> State:
    """Save the final approved code"""
    # Define the folder path relative to the script location
    generated_code = state["generated_code"]
    agent_name = state["agent"].name
    create_local_repository(agent_code=generated_code, agent_name=agent_name)

    return {
        "code_instructions": state["code_instructions"],
        "generated_code": state["generated_code"],
        "required_libraries": state["required_libraries"],
        "message": "Agent has been successfully created!",
        "step": "done"
    }

def get_user_feedback(state: State) -> State:
    """Get human feedback on the generated code"""
    # Interrupt the workflow to get human feedback

    if state["step"] == "find_agent":
        question = f"Is {state['agent'].name} the correct agent?\n\n```python \r{state['agent'].code}"
        feedback = interrupt(
            {
                "question": question
            }
        )

    elif state["step"] == "update_code":
        question = f"```python \r{state["generated_code"]}```\n\n##Do you approve this code:\n"
        feedback = interrupt(
            {
                "question": question
            }
        )
    elif state["step"] == "libraries" and state["required_libraries"] is not None:
        question = f"Do you approve these additional libraries:\n ```\r{'\n'.join(state['required_libraries'])}"
        feedback = interrupt(
            {
                "question": question
            }
        )
    elif state["step"] == "libraries" and state["required_libraries"] is None:
        updated_state = state.copy()
        updated_state["approve"] = True
        return updated_state
    
    elif state["step"] == "name":
        question = f"Can we call your agent {state['suggested_name']}?"
        feedback = interrupt(
            {
                "question": question
            }
        )

    else:
        raise ValueError(f"Invalid step for user feedback => state:{state['step']} feedback:{feedback}")

    message = f"""Read the user's feedback and determine if the code is approved.
     If the code is not approved, extract EXACT WORDS VERBATIM from user's message for updated code requirements.
     feedback: {feedback}"""

    user_feedback = completions_structured(message=message, response_format=UserFeedback)
    
    # Store the approval status and updated requirements in the state
    updated_state = state.copy()
    updated_state["approve"] = user_feedback.approve
    
    if not user_feedback.approve:
        # If not approved, update the requirements
        updated_state["user_feedback"] = user_feedback.user_feedback
    
    # Return the updated state - the router will handle the branching
    return updated_state

# Create the workflow graph
def create_agent_workflow():
    # Initialize the graph
    graph = StateGraph(State)
    
    # Add nodes
    graph.add_node("find_agent", find_agent)
    graph.add_node("update_code", update_code)
    graph.add_node("get_user_feedback", get_user_feedback)
    graph.add_node("generate_required_libraries", generate_required_libraries)
    graph.add_node("write_required_libraries", write_required_libraries)
    graph.add_node("write_code", write_code)
    
    # Add edges
    graph.add_edge(START, "find_agent")
    graph.add_edge("find_agent", "get_user_feedback")
    graph.add_edge("update_code", "get_user_feedback")
    graph.add_edge("generate_required_libraries", "get_user_feedback")
    
    # Define the conditional edge function
    def conditional_transition(state):
        approve = state.get("approve")
        step = state.get("step")

        if approve is True and step == "find_agent":
            return "update_code"
        elif approve is False and step == "find_agent":
            return "find_agent"
        elif approve is True and step == "update_code":
            return "generate_required_libraries"
        elif approve is False and step == "update_code":
            return "update_code"
        elif approve is True and step == "libraries" and state["required_libraries"] is not None:
            return "write_required_libraries"
        elif approve is True and step == "libraries" and state["required_libraries"] is None:
            return "write_code"
        elif approve is False and step == "libraries":
            return "update_code"
        else:
            raise ValueError(f"Unhandled state: approve={approve}, step={step}")

    # Add the conditional edges
    graph.add_conditional_edges(
        "get_user_feedback",
        conditional_transition
    )
    
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

# Global storage for in-progress workflows
active_workflows = {}

#@tool
def workflow_update_agent(
    workflow_id: Optional[str] = None,
    messages: Optional[list[str]] = None,
) -> Generator[str, None, None]:
    """
    1) Call this tool when the user wants to update an agent or agent's code.
    2) Consider this tool when the messages contains a workflow_id value.
    3) DO NOT assign a value to the workflow_id parameter, unless it is given in the message.

    Parameters:
    - workflow_id (str, optional): Only populate this field when the workflow_id is given in the messages.
    - messages: (Optional) Leave this field blank; it will be automatically populated with the user input.
    """
    try:
        # get the last user message
        last_user_message = get_last_user_message(messages)
        if not last_user_message:
            yield "No user message found. Please provide a message."
            return

        # Define stream variable outside the if/else blocks
        stream = None
        
        # If we have a workflow_id, we're resuming an existing workflow
        if workflow_id and workflow_id in active_workflows:
            workflow = active_workflows[workflow_id]["workflow"]
            thread_id = active_workflows[workflow_id]["thread_id"]
            
            # Use stream instead of invoke for consistency
            stream = workflow.stream(
                Command(resume=last_user_message),
                config={"configurable": {"thread_id": thread_id}}
            )
            
        else:
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
                    "agent_name": None,
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