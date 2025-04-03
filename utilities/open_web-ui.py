import json
from typing import Generator
from agents.openai import completions_structured
from agent_tooling import tool
from models.open_web_ui import Autocompletion, Summary, Tags

@tool
def message_autocomplete(message: str) -> Generator[str, None, None]:
    """Suggests autocompletions for the given message."""
    autocompletion = completions_structured(message=message, response_format=Autocompletion)
    yield json.dumps({"text": autocompletion.text})

@tool
def create_title_and_emoji(message: str) -> Generator[str, None, None]:
    """Creates a title for the chat thread with an emoji."""
    summary = completions_structured(message=message, response_format=Summary)
    yield json.dumps({"title": summary.title})

@tool
def create_tags(message: str) -> Generator[str, None, None]:
    """Creates tags categorizing the main themes of the chat history."""
    tags = completions_structured(message=message, response_format=Tags)
    yield json.dumps({"tags": tags.tags})