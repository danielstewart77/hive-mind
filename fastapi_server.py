from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from shared.state import editor_state
import os
import logging
import uvicorn
import json

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

# Create the standalone FastAPI app
app = FastAPI()
templates = Jinja2Templates(directory="templates")

@app.get("/edit", response_class=HTMLResponse)
async def edit_page(request: Request):
    try:
        with open("/tmp/editor_state.json", "r") as f:
            data = json.load(f)
            file_path = data.get("file_path")
    except FileNotFoundError:
        return HTMLResponse("No file selected or file not found.", status_code=404)

    if not file_path or not os.path.exists(file_path):
        return HTMLResponse("No file selected or file not found.", status_code=404)

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    return templates.TemplateResponse("edit.html", {
        "request": request,
        "file_name": os.path.basename(file_path),
        "file_content": content
    })

@app.post("/save")
async def save_code(request: Request):
    body = await request.json()
    code = body.get("code", "")
    file_path = editor_state.get("file_path")

    if not file_path:
        return JSONResponse({"error": "No file set."}, status_code=400)

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)
        return {"message": "Saved."}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    
if __name__ == "__main__":
    log.info("Starting FastAPI server on http://localhost:7779")
    uvicorn.run("fastapi_server:app", host="localhost", port=7779, reload=True)
