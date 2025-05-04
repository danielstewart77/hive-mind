import json
from typing import Generator, Optional
from utilities.openai_tools import completions_structured
from agent_tooling import tool
from models.open_web_ui import Autocompletion, Summary, Tags

@tool
def message_autocomplete(messages: Optional[list[str]] = None) -> Generator[str, None, None]:
    """
    Suggests autocompletions for the given message.

    Args:
        message: (Optional) Leave this field blank; it will be automatically populated with the user input.
    """
    pass
    # last_user_message = next(
    #     (m["content"] for m in reversed(messages) if m["role"] == "user"),
    #     ""
    # )
    # autocompletion = completions_structured(message=last_user_message, response_format=Autocompletion)
    # yield json.dumps({"text": autocompletion.text})

@tool
def create_title_and_emoji(messages: Optional[list[str]] = None) -> Generator[str, None, None]:
    """Creates a title for the chat thread with an emoji.
    Args:
        message: (Optional) Leave this field blank; it will be automatically populated with the user input.
    """
    pass
    # last_user_message = next(
    #     (m["content"] for m in reversed(messages) if m["role"] == "user"),
    #     ""
    # )
    # summary = completions_structured(message=last_user_message, response_format=Summary)
    # yield json.dumps({"title": summary.title})

@tool
def create_tags(messages: Optional[list[str]] = None) -> Generator[str, None, None]:
    """Creates tags categorizing the main themes of the chat history.
    Args:
        message: (Optional) Leave this field blank; it will be automatically populated with the user input.
    """
    pass
    # last_user_message = next(
    #     (m["content"] for m in reversed(messages) if m["role"] == "user"),
    #     ""
    # )
    # tags = completions_structured(message=last_user_message, response_format=Tags)
    # yield json.dumps({"tags": tags.tags})