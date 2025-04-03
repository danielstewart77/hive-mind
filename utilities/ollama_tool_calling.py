# import os
# import json
# import requests
# from dotenv import load_dotenv
# #from your_tool_wrapper import get_tools
# #from services.openai_tool_wrapper import get_tools
# from agent_tooling import get_tool_schemas, get_tool_function

# from dotenv import load_dotenv
# load_dotenv(dotenv_path='secrets.env')

# OLLAMA_API_ADDRESS = os.getenv('OLLAMA_API_ADDRESS')
# OLLAMA_API_URL = f'http://{OLLAMA_API_ADDRESS}:11434/api'
# MODEL_NAME = "llama3.2:3b"  # Change this based on your model
# #MODEL_NAME = "mistral"
# #MODEL_NAME = "mistral-small:22b"
# #MODEL_NAME = "mixtral:8x22b"


# def call_tools_ollama(messages):
#     #tools, available_functions = get_tools()
#     tools = get_tool_schemas()
    
#     messages.append({
#         "role": "system",
#         "content": f'''Choose every tool in this list that would be useful for solving the task.
#           You can choose multiple tools from the list below:
#           {tools}'''
#     })

#     # append messages that tell the model that it MUST choose one of the tools given and cannot
#     # choose a tool that is not in the list
#     messages.append({
#         "role": "system",
#         "content": '''If you choose one of the tools in the list above, you MUST use
#           the EXACT tool name and arguments as they appear in the list above.'''
#     })

#     payload = {
#         "model": MODEL_NAME,
#         "messages": messages,
#         "tools": tools,
#         "stream": False
#     }

#     response = requests.post(f"{OLLAMA_API_URL}/chat", json=payload).json()

#     choice = response.get("message", {})
#     tool_calls = choice.get("tool_calls", [])

#     # Check for tool calls
#     if tool_calls:
#         for tool_call in tool_calls:
#             function_name = tool_call['function']['name']
#             arguments = tool_call['function']['arguments']

#             function_to_call = get_tool_function(function_name)
#             if function_to_call:
#                 result = function_to_call(**arguments)
#                 messages.append({
#                     "role": "tool",
#                     "name": function_name,
#                     "content": json.dumps(result)
#                 })

#         # Make another call to Ollama to generate the final response
#         final_payload = {
#             "model": MODEL_NAME,
#             "messages": messages,
#             "stream": False
#         }
#         final_response = requests.post(f"{OLLAMA_API_URL}/chat", json=final_payload).json()
#         final_message = final_response.get("message", {})
#         return {"response": final_message.get("content", "")}

#     # If no tool calls, just return the model's direct response
#     return {"response": choice.get("content", "")}


# # Example usage
# # response = call_llm([{ "role": "user", "content": "Your user query here" }])
