import logging
import os
from typing import Generator

from openai import completions
from utilities.openai_tools import completions_streaming, completions_structured, completions
from agent_tooling import tool

from models.maker import AgentCode, AgentName, RequirementUpdate, RequiredLibrariesUpdate

#@tool
def generate_agent_code(agent_requirements: str, llm_provider: str = "openai") -> str: 
    """Is called when the user specifically says they want to create a NEW agent.
    This function creates a new agent based on the provided requirements and language model provider.
    Args:
        agent_requirements (str): the requirements for the agent
        llm_provider (str): the provider of the language model to use e.g, openai, ollama, huggingface
    Returns:
        str: a message indicating the agent was created successfully
    """

    if llm_provider == "openai":
        llm_model = "gpt-4.1-mini"

    # *** create the project *** #

    # add @tool decorator to the function
    # add a verbose description of the function
    # ***** make sure the agent knows to descripbe the function is a distince way that will help the 
    # ***** triage agent know if this is the correct function to call

    instructions = f"Create a python function with the following requirements: {agent_requirements}"

    agent_code = completions_structured(message=instructions,
                                        response_format=AgentCode,
                                        model=llm_model,
    ).code

    instructions = f''' Update this code: {agent_code} by adding the following:
        1) import the library `from agent_tooling import tool`
        2) add the `@tool` decorator to the main/entry-point function
        3) add an extra parameter to the function with the signature: messages: list[dict[str, str]] = None'''
    
    agent_code = completions_structured(message=instructions,
                                        response_format=AgentCode,
                                        model=llm_model,
    ).code

    instructions = f''' Update this code: {agent_code} by adding the following:
        add a description of the function below the function less than 1024 characters
        1) this description needs to help an llm determine if this is the correct function to call
        for the given request
        2) the description should also help a human understand what the function does'''
    
    agent_code = completions_structured(message=instructions,
                                        response_format=AgentCode,
                                        model=llm_model,
    ).code

    instructions = f''' Update this code: {agent_code} by adding the following:
        1) assume that the tool calling function will send arguments as strings, 
        so verify datatypes and attempt to convert strings to the correct type
        2) add datatype hints to each argument in the function (if not already there)'''
        
    agent_code = completions_structured(message=instructions,
                                        response_format=AgentCode,
                                        model=llm_model,
    ).code

    # instructions = f''' If the this code is using secret keys, passwords, or tokens: {agent_code}:
    #     use this pattern: 
    #     from dotenv import load_dotenv
    #     import os
    #     load_dotenv(dotenv_path='secrets.env')
    #     KEY_NAME = os.getenv("KEY_NAME")
    #     USERNAME = os.getenv("USERNAME")
    #     PASSWORD = os.getenv("PASSWORD")'''

    # agent_code = completions_structured(message=instructions,
    #                                     response_format=AgentCode,
    #                                     model=llm_model,
    # ).code

    instructions = f''' Update this code: {agent_code} by using the following pattern to return
    a streaming value:
        1) if the function is going to return something complicated
        call the completions_streaming function with a message that will help the llm produce a nice response for the user:
        2) otherwise, return a generator function that yields the result or a success message
        3) don't forget to add from typing import Generator

    Example:
    from typing import Generator
    from agents.openai import completions_streaming
    
    message = "Format this message nicely: The sum of num_a, num_b, and num_c is result."
        
        stream = completions_streaming(message=message)

        # stream the response
        for chunk in stream:
            yield chunk
    '''

    return completions_structured(message=instructions,
                                        response_format=AgentCode,
                                        model=llm_model,
    ).code

def generate_requirements(agent_code: str, llm_model: str = "gpt-4.1-nano") -> list[str] | None:
    file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "requirements.txt")

    with open(file, "r") as f:
        requirements = f.readlines()
        requirements = [r.strip() for r in requirements]

    libraries_update = completions_structured(
            message=f'''Based on this code: {agent_code} \n Does the contents of the requirements.txt file:
            \n {requirements} need to be updated? If so, list each missing requirement.''',
            model=llm_model,
            response_format=RequiredLibrariesUpdate
    )

    if libraries_update.update:
        return libraries_update.libraries
    else:
        return None
    
def extract_function_name(agent_code: str, llm_model: str = "gpt-4.1-nano") -> str:
    agent_name_suggestion = completions_structured(
        message=f"""Return the name of the function decorated with `@tool` in this code: {agent_code}""",
        model=llm_model,
        response_format=AgentName).name
    return agent_name_suggestion
    
def update_requirements(required_libraries: list[str]) -> None:
    # update the requirements.txt file with the required libraries
    file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "requirements.txt")
    with open(file, "a") as f:
        for req in required_libraries:
            f.write(f"\n{req}")
            logging.info(f"Updated requirements.txt file: {file}")
    
    # run pip install -r requirements.txt
    # if not in docker
    if not os.path.exists("venv"):
        # activate the virtual environment
        if os.name == 'nt':
            os.system("venv\\Scripts\\activate")
        else:
            os.system(". venv/bin/activate")
    os.system("pip install -r requirements.txt")

def create_local_repository(agent_code: str, agent_name: str) -> None:
    # Define the folder path relative to the script location
    folder = os.path.dirname(__file__)
    file = os.path.join(folder, f"{agent_name}.py")

    # Ensure the directory exists
    if not os.path.exists(folder):
        os.makedirs(folder)
        logging.info(f"Created directory: {folder}")

    # Write the generated code to the main file
    with open(file, "w") as f:
        logging.info(f"Writing code to file: {file}")
        f.write(agent_code)