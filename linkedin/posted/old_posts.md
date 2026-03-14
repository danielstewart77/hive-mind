In order from newest to oldest:

🚀 New features just dropped for hive_mind, my AI agent workbench project:

🧰 1. MCP Bootstrapping with agent_tooling
 I've wired up my MCP server to dynamically load all available agents using the agent_tooling registry. Zero boilerplate. Instant tool exposure.

# Register all tools with FastMCP
for schema in get_tool_schemas():
 name = schema['name']
 func = get_tool_function(name)
 mcp.tool()(func)

🔌 2. Open WebUI Integration via mcpo
 That same MCP server can now be wrapped as a mcpo endpoint. Open WebUI picks up tools from it and routes calls seamlessly.

📸 Screenshot shows two frontends hitting the same tool:
> On the left: Open WebUI using gpt-4.1-nano via MCP
>On the right: Gradio frontend using a local granite3.3:2b model

https://lnkd.in/gMhBvV4h

hashtag#FastMCP hashtag#MCP hashtag#mcpo hashtag#OpenWebUI hashtag#gradio

--------------------------------------------------------------------------------------------------------------

🚀 New hive_mind Update: agent testing workflows just leveled up
 Just pushed a major upgrade to hive_mind, my sandbox for building, testing, and orchestrating modular AI agents. This one’s a big step forward.

🎥 In this video:
 I demo how agent_tooling helps agents inspect local Git changes and auto-generate a LinkedIn post like this one based on the commit history. Agents helping devs document agents. Pretty meta.

🧠 What’s new in hive_mind:
 ✅ Multi-step workflows via LangGraph stage graphs – the workflow_create_agent is now smarter and smoother
 ✅ Gradio & CLI interfaces – run agents in your browser or terminal
 ✅ Full agent_tooling integration – including:
 🔹 @tool decorators with tag-based discovery
 🔹 Seamless OpenAI + Ollama tool calling
 🔹 Tag-based tool filtering and tool fallback support
🔧 Powered by agent_tooling — my Python library for building modular, schema-aware toolchains for agents.

→ Try it out:
pip install --upgrade agent_tooling
→ Project: https://lnkd.in/gMhBvV4h

Still under 500 lines of core logic. Still focused. Now orchestrating even more powerful agents.

hashtag#HuggingFace hashtag#Gradio hashtag#AgentOrchestration hashtag#LangGraph hashtag#OpenAI hashtag#Ollama hashtag#AIEngineering hashtag#LLMTools

--------------------------------------------------------------------------------------------------------------

🚀 New agent_tooling Update: Local Model Support with OllamaTooling!!!

Just released another significant update to agent_tooling, the Python library I use to register, tag, discover, and call agent tools.

This update brings seamless integration with local models via Ollama, enhancing flexibility and performance for AI workflows.

🧠 What’s new in this release:
✅ OllamaTooling – Leverage local models served by Ollama with the same ease as OpenAI's API.
✅ Tool Routing – Automatically select the appropriate tool based on tags, ensuring efficient processing.
✅ Fallback Mechanism – Define backup functions to handle cases where no tools match, enhancing resilience.
✅ Streaming Support – call_tools() now returns a generator for improved stream handling.

This update empowers agents to utilize local models effectively, providing greater control and reducing reliance on external APIs.

→ Check it out: GitHub - https://lnkd.in/gDttaMB2
→ Install or upgrade:
pip install agent_tooling --upgrade

Still under 500 lines of logic. Still focused. Now even more capable.

hashtag#Ollama hashtag#AgentOrchestration hashtag#LangGraph hashtag#Python hashtag#LLMTools hashtag#AIEngineering

--------------------------------------------------------------------------------------------------------------

> New hive_mind Update: create agent workflow with LangGraph

> I've enhanced hive_mind by converting the maker agent (responsible for creating new agents) into a structured workflow using LangGraph. This workflow-driven approach provides precise orchestration, robust continuity, and improved human-in-the-loop interactions.

> Why I'm going all in on orchestration: https://lnkd.in/gaMevtjp
> Take a look at the branch on GitHub: https://lnkd.in/gKYzCiTX

> Check out the new workflow in action—creating and immediately using a new agent. 👇 

hashtag#OpenWebUI hashtag#LangGraph hashtag#AI hashtag#AgentOrchestration



New hive_mind Update: CRUD Operations for Agent Management
I've added CRUD (Create, Read, Update, Delete) functionality to hive_mind using the updated agent_tooling library.

What's New:
- Dynamically create new agents using structured definitions.
- Read existing agents, including retrieving agent lists and their code.
- Update agent definitions and behaviors through code changes.
- Delete agents as needed.

Enhancements:
- Improved agent interactions with streaming responses.
- Better tooling discovery and management capabilities.

Upcoming Integrations:
- LangGraph: This is promising solution for structuring agent workflows, particularly to decrease reliance on LLM-based task ordering and include human-in-the-loop decision-making. Initial demos were successful, and I'm planning the first major integration.
- Model Context Protocol (MCP): MCP has gained traction recently, though I'm still evaluating its long-term viability compared to the OpenAPI standard (referenced by the Open WebUI project: why OpenAPI: https://lnkd.in/gtUPm5tB, why mcpo: https://lnkd.in/gAFChC-m). I'll experiment with both and decide which suits my needs best.

More hive_mind specifics in my latest article: https://lnkd.in/gCaV-SeH

GitHub hive_mind: https://lnkd.in/ghrBcfx9

GitHub agent_tooling: https://lnkd.in/g-nrPdKx

hashtag#OpenWebUI hashtag#LangGraph hashtag#AI hashtag#MCP hashtag#OpenAPI

--------------------------------------------------------------------------------------------------------------

🚀 New Feature: Seamless OpenAI Integration in agent_tooling!
With this update, you can now register Python functions as tools and let OpenAI models handle function calling effortlessly.

🔹 What’s New?
✅ OpenAITooling class – Simplifies OpenAI API interactions by automatically handling tool calls.
✅ Schema-compatible tools – Functions are registered in OpenAI-compatible formats with no extra work.
✅ Automatic function invocation – OpenAI can now trigger your registered tools based on user queries.

✨ How It Works
1️⃣ Register functions as tools
2️⃣ Tools are available to OpenAITooling
3️⃣ OpenAITooling decides which tools to use and calls them automatically!

📌 See it in action in this google colab: https://lnkd.in/gBhkQ2FU

Check out the project:
GitHub: https://lnkd.in/gMhBvV4h
PyPI: https://lnkd.in/gaBuXJKy

🔥 Coming Next: hive_mind agent CRUD operations!
My next release for hive_mind we’ll use agent_tooling to build a CRUD stack for agents.
With hive_mind, you’ll be able to:
🆕 Create new agents on the fly & hive_mind will immediately recognize them
📖 Read existing agents & ask what they do
✏️ Update agents dynamically by making code changes
❌ Delete agents you no longer need
This will enable truly flexible and adaptive agent-based systems! Stay tuned. 
👉 Check out the repo here: https://lnkd.in/gMhBvV4h

--------------------------------------------------------------------------------------------------------------

🚀 Agent Collaboration, Simplified with agent_tooling! 🤖🔧

Excited to announce an update to my AI agent project, now powered by agent_tooling! This Python package provides an easy, lightweight way to register functions with metadata, making dynamic tool selection and inter-agent communication easier.

What's New:
Dynamic Tool Registration: Similar to Hugging Face's transformers.tools.Tool, but framework-agnostic.
Context-Aware Tool Selection: Agents now intelligently choose tools based on conversation context, greatly enhancing flexibility.

Key Features:
🐍 Framework-Agnostic & Lightweight: Easy integration into any Python-based AI project.
🌐 Open Web-UI Integration: Seamlessly handle requests from Open Web-UI.
🧩 Pydantic Integration: Robust data validation and structured responses.
📦 Docker & Ollama Ready: Supports local execution and containerized environments for flexibility.
📜 Maintained Chat Threads: Maintains standard chat thread for chatbot integration.

Check out the project here:
GitHub: https://lnkd.in/gMhBvV4h
agent_tooling PyPi package: https://lnkd.in/gaBuXJKy

Looking forward to your thoughts and feedback—contributions are always welcome! 🚀

--------------------------------------------------------------------------------------------------------------

🚀 Introducing agent_tooling - A Simple and Powerful Python Tool Decorator! 🔧

I've just published agent_tooling, a Python package that provides a streamlined way to register functions with metadata—similar to Hugging Face's transformers.tools.Tool but lightweight and framework-agnostic.

Check it out here: https://lnkd.in/gaBuXJKy or here: https://lnkd.in/gDttaMB2
And see Hugging Face's approach: https://lnkd.in/gvQitMDU

This package will soon play a key role in a new agent orchestration project I’m working on. Previously, I stored agent tool definitions in a database so the orchestrator agent could dynamically retrieve and interact with them. This time, I'll be redoing the project with agent_tooling to demonstrate a system where agents maintain awareness of other agents in the network, making inter-agent collaboration even more seamless.

Stay tuned for the GitHub repo drop and a deeper dive into how this package enables more efficient AI workflows!