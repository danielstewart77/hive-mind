import json
import os
from agent_tooling import tool
from dotenv import load_dotenv
import psutil
import platform
import socket
from typing import Any, Dict, Generator
from utilities.messages import get_last_user_message
from utilities.openai_tools import completions_streaming
from openai import OpenAI


@tool(tags=["triage"])
def get_system_info(messages: list[dict[str, str]]) -> Generator[str, None, None]: 
    """
    Call this function if the user asks for system or server information, this function is called to gather and return the system's details.
    It collects CPU usage, memory usage, disk utilization, OS name, platform details, CPU architecture, 
    and hostname of the system in a structured format.
    """
    # Notify via completions_streaming that system info is being fetched.
    #yield "Fetching system information..."
    print("Fetching system information...")
    print("📡 Inside get_system_info()")

    # Gathering system details.
    cpu_usage = psutil.cpu_percent(interval=1)
    memory_info = psutil.virtual_memory()
    memory_usage = {"total": memory_info.total, 
                    "available": memory_info.available, 
                    "percent": memory_info.percent, 
                    "used": memory_info.used, 
                    "free": memory_info.free}
    disk_usage = psutil.disk_usage('/')
    os_name = platform.system()
    platform_details = platform.platform()
    cpu_architecture = platform.machine()
    hostname = socket.gethostname()

    # Stream the gathered system info back to the client.
    system_info = {
        "cpu_usage": cpu_usage,
        "memory_usage": memory_usage,
        "disk_usage": {"total": disk_usage.total, 
                        "used": disk_usage.used, 
                        "free": disk_usage.free, 
                        "percent": disk_usage.percent},
        "os_name": os_name,
        "platform_details": platform_details,
        "cpu_architecture": cpu_architecture,
        "hostname": hostname,
    }

    load_dotenv(dotenv_path='secrets.env')
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

    message = get_last_user_message(messages)

    stream = client.responses.create(
        model="gpt-4.1",
        input=f"{message}: {json.dumps(system_info)}",
        stream=True
    )

    for event in stream:
        # Check if the event has a 'delta' attribute
        if hasattr(event, 'delta'):
            delta = getattr(event, 'delta', '')
            if delta:
                yield delta