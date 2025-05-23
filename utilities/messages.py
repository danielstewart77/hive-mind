def get_last_user_message(messages: list[dict[str, str]]) -> str:
    """
    Extracts the last message from a list of messages.
    
    Args:
        messages (list[dict[str, str]]): A list of message dictionaries.
        
    Returns:
        str: The content of the last message in the list.
    """
    return next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"),
            ""
        )

def get_last_message(messages: list[dict[str, str]]) -> str:
    """
    Extracts the last message from a list of messages.
    
    Args:
        messages (list[dict[str, str]]): A list of message dictionaries.
        
    Returns:
        str: The content of the last message in the list.
    """
    return messages[-1]["content"] if messages else ""

def get_last_assistant_message(messages: list[dict[str, str]]) -> str:
    """
    Extracts the last assistant message from a list of messages.
    
    Args:
        messages (list[dict[str, str]]): A list of message dictionaries.
        
    Returns:
        str: The content of the last assistant message in the list.
    """
    return next(
            (m["content"] for m in reversed(messages) if m["role"] == "assistant"),
            ""
        )

def get_last_system_message(messages: list[dict[str, str]]) -> str:
    """
    Extracts the last system message from a list of messages.
    
    Args:
        messages (list[dict[str, str]]): A list of message dictionaries.
        
    Returns:
        str: The content of the last system message in the list.
    """
    return next(
            (m["content"] for m in reversed(messages) if m["role"] == "system"),
            ""
        )

def get_last_function_message(messages: list[dict[str, str]]) -> str:
    """
    Extracts the last function message from a list of messages.
    
    Args:
        messages (list[dict[str, str]]): A list of message dictionaries.
        
    Returns:
        str: The content of the last function message in the list.
    """
    return next(
            (m["content"] for m in reversed(messages) if m["role"] == "function"),
            ""
        )


def mentions_tag(messages: list[dict[str, str]], tag: str, roles: list[str]) -> bool:
    tag_lower = tag.lower()
    return any(
        tag_lower in m["content"].lower()
        for m in reversed(messages)
        if m["role"] in roles
    )

def mentions_editor(messages: list[dict[str, str]]) -> bool:
    return mentions_tag(messages, "@editor", roles=["user", "function"])
