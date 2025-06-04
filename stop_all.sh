#!/bin/bash

echo "Stopping Gradio..."
pkill -f gradio_app.py

echo "Stopping FastAPI..."
pkill -f fastapi_server:app

echo "Stopping MCP server..."
pkill -f mcpo

echo "All services stopped."
