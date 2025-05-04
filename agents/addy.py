from typing import Generator
from agent_tooling import tool
from utilities.openai_tools import completions_streaming

@tool
def addy(a: str, b: str, c: str, messages: list[dict[str, str]] = None) -> Generator[str, None, None]:
    """
    Adds three numbers a, b, and c and streams a nicely formatted message with the result.

    This function accepts three inputs as strings, attempts to convert them into floats, and computes their sum.
    It returns the response as a streaming output, formatted by an LLM completion call.

    Use this function when you need to add three numeric values given as strings and want a clear,
    human-readable explanation of the addition result streamed from an LLM.

    The optional messages parameter provides context for the LLM but is not used in the calculation itself.

    This description helps an LLM to determine if this function matches a query involving adding three values,
    and helps humans understand the function's purpose and outputs.
    """
    # Verify and convert inputs to floats
    if not isinstance(a, str):
        raise TypeError(f"Input 'a' must be a string representing a number, got {type(a).__name__}")
    if not isinstance(b, str):
        raise TypeError(f"Input 'b' must be a string representing a number, got {type(b).__name__}")
    if not isinstance(c, str):
        raise TypeError(f"Input 'c' must be a string representing a number, got {type(c).__name__}")

    try:
        a_num = float(a)
    except (ValueError, TypeError):
        raise ValueError(f"Input 'a' must be a number or numeric string, got {a}")
    try:
        b_num = float(b)
    except (ValueError, TypeError):
        raise ValueError(f"Input 'b' must be a number or numeric string, got {b}")
    try:
        c_num = float(c)
    except (ValueError, TypeError):
        raise ValueError(f"Input 'c' must be a number or numeric string, got {c}")

    result = a_num + b_num + c_num

    message = f"Format this message nicely: The sum of {a_num}, {b_num}, and {c_num} is {result}."
    stream = completions_streaming(message=message)

    for chunk in stream:
        yield chunk
