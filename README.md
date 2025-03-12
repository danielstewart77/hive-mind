# Agent Tooling Demonstration

This project demonstrates an implementation of the `agent_tooling` package by utilizing a model's tool calling capabilities.

## Features
- **Implements @tool decorator**: Example implementation of tool registration.
- **Tool Calling**: Demonstrates how an agent dynamically selects tools based on context.

## Misc Features
- **Open Web-UI Integration**: Integrates (pretty well...) with Open Web-UI requests.
- **Task Decomposition**: Uses an LLM to break down complex user queries into structured steps.
- **Pydantic Integration**: Ensures structured output with JSON validation.
- **OpenAI API Integration**: Calls models like `gpt-4o` for automatica tool selection.
- **Ollama API Integration**: Calls models like `deepseek-r1:14b` and `deepseek-coder-v2:16b` for various tasks.
- **Chat Threading**: Maintains a structured conversation history to determine tool execution flow.
- **Answer Completion Evaluation**: Determines if the response fully answers the user's query.

## Installation

### Prerequisites
- Python 3.8+
- Docker (for running Ollama)
- `agent_tooling` package
- Pydantic for JSON validation
- dotenv for environment variable management

### Setup
1. Clone this repository:
   ```bash
   git clone https://github.com/danielstewart77/hive_mind.git
   cd hive_mind
   ```
2. Create virtual environment:
    ```
    python3 -m venv venv
    ```
2. Install dependencies:
    - Activate your environment (linux)
    ```bash
    . venv/bin/activate
    ```
    - Activate your environment (windows)
    ```bash
    . .\venv\Scripts\activate
    ```
    - Install requirements
    ```bash
    pip install -r requirements.txt
    ```
3. Set up environment variables:
   - Create a `secrets.env` file and define:
     ```ini
     OLLAMA_API_ADDRESS=<your ollama api address>
     OPENAI_API_KEY=<you open ai api key>
     ```

4. Run the project locally:
    - launch the `main.py` file
    ```bash
    python3 main.py
    ```
5. (Optional) Run as a container:
    - if you want to use open web-ui, uncomment it's configuration in `docker-compose.yaml`
    - start the docker container
   ```bash
   docker compose up -d --build
   ```

## Usage

### Running the API
Start the Flask/Quart application:
```bash
python app.py
```

### Example Requests
```json
{"messages":[
  {"role": "user", "content":"what is the curren bitoin price"}
]}
```

#### Example Response
```json
{
    "response": [
        "Agent CoinGecko 🦎: The current price of Bitcoin is $82779 USD."
    ]
}
```

## Project Structure
```
HIVE_MIND/
│── .vscode/
│── agents/
│   │── __pycache__/
│   │── agent_0.py
│   │── coingecko.py
│   │── large_tasks.py
│   │── ollama.py
│   │── openai.py
│── models/
│   │── __pycache__/
│   │── open_web_ui.py
│── open_web-ui_system_prompts/ # If you're using Open Web-UI, these will help you understand how it's sending requests.
│   │── message_autocompletion.txt
│   │── message_summery.txt
│   │── message_tags.txt
│── services/
│   │── __pycache__/
│   │── tool_calling.py
│   │── agent_tooling.py # Here's where `agent_tooling.get_registered_tools()` is used to provide `gpt-4o` with available tools
│── venv/
│   │── __init__.py
│── .dockerignore
│── .gitignore
│── .gitignore copy
│── docker-compose.yml
│── Dockerfile
│── main.py
│── README.md
│── requirements.txt

```

## Contributing
Feel free to submit issues or pull requests for improvements!

## License

This is free and unencumbered software released into the public domain.

Anyone is free to copy, modify, publish, use, compile, sell, or distribute this 
software, either in source code form or as a compiled binary, for any purpose, 
commercial or non-commercial, and by any means.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR 
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, 
FITNESS FOR A PARTICULAR PURPOSE, AND NONINFRINGEMENT. IN NO EVENT SHALL THE 
AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES, OR OTHER LIABILITY, WHETHER IN AN 
ACTION OF CONTRACT, TORT, OR OTHERWISE, ARISING FROM, OUT OF, OR IN CONNECTION 
WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

For more information, please refer to <https://unlicense.org/>