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