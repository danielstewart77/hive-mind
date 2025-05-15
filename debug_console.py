import os
from typing import Generator
from agent_tooling import OpenAITooling, discover_tools
from workflows.root import root_workflow
from shared.state import stream_cache

# Initialize tooling
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai = OpenAITooling(api_key=OPENAI_API_KEY, model="gpt-4o")
discover_tools(['agents', 'workflows', 'utilities'])

# Global chat history in OpenAI format
chat_history = []

def chat_interface(user_message: str) -> Generator[str, None, None]:
    global chat_history

    user_msg = {"role": "user", "content": user_message}
    chat_history.append(user_msg)

    response_stream = root_workflow(messages=chat_history)

    full_response = ""
    thread_id = None
    last_state = None

    # First stage of streaming
    for partial in response_stream:
        if isinstance(partial, dict):
            last_state = next(reversed(partial.values()), {})
            thread_id = last_state.get("thread_id")
            result = last_state.get("result", "")
            full_response += result or ""
        else:
            full_response += str(partial)

        yield full_response.strip()

    # Second stage: Streaming from cached generator
    if thread_id and thread_id in stream_cache:
        cached_stream = stream_cache.pop(thread_id)
        for item in cached_stream:
            try:
                if hasattr(item, "choices") and item.choices[0].delta:
                    content = item.choices[0].delta.content
                elif isinstance(item, str):
                    content = item
                else:
                    content = str(item)
            except Exception as e:
                print("⚠️ Error extracting content from stream:", e)
                continue

            if content:
                full_response += content
                yield full_response.strip()

    # Final message
    if full_response:
        chat_history.append({"role": "assistant", "content": full_response.strip()})
    else:
        # Try to extract fallback from function message
        message = None
        if last_state and isinstance(last_state, dict):
            messages = last_state.get("messages", [])
            for msg in reversed(messages):
                if "content" in msg:
                    message = msg["content"]
                    break

        chat_history.append({
            "role": "assistant",
            "content": message.strip() if message else "[no response]"
        })
        yield message.strip() if message else "[no response]"

def main():
    print("💬 Chat with the Hive Mind (type 'exit' to quit)\n")
    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in {"exit", "quit"}:
            print("👋 Goodbye!")
            break

        for chunk in chat_interface(user_input):
            print(f"\rHive: {chunk}", end="", flush=True)
        print("\n")  # Newline after the streaming completes

if __name__ == "__main__":
    main()
