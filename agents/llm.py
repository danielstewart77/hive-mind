from typing import Generator
from agent_tooling import tool
from utilities.openai_tools import completions_with_messages

@tool(tags=["triage"])
def big_dog_llm(str, messages: list[dict[str,str]]) -> Generator[str, None, None]:
    """call this agent to deal with confusing, large, or complicated tasks including logical sorting and list making"""

    stream = completions_with_messages(
        messages=messages,
        model="gpt-4.1",
        stream=True
    )

    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content is not None:
            yield chunk.choices[0].delta.content