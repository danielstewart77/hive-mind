#!/bin/bash

# Activate the virtual environment
source venv/bin/activate

# Start Gradio app
nohup ./venv/bin/python gradio_app.py > logs/gradio.log 2>&1 &

# Start FastAPI server
nohup ./venv/bin/uvicorn fastapi_server:app --host 0.0.0.0 --port 7779 --reload > logs/fastapi.log 2>&1 &

# Start MCP server via mcpo
nohup ./venv/bin/mcpo --port 7777 -- python mcp_server.py > logs/mcp.log 2>&1 &

echo "All servers started. Logs are in the logs/ directory."
