from agent_tooling import tool
from utilities.openai_tools import completions_with_messages

@tool(tags=["triage"])
def big_dog_llm(self, question: str, messages: list[dict[str,str]]):
    """call this agent to deal with confusing, large, or complicated tasks including logical sorting and list making"""

    result = completions_with_messages(
        messages=messages,
        model="gpt-4.1",
    )

    return result