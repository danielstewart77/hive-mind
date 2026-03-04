import json
import os
import requests
from agent_tooling import tool
from agents.secret_manager import get_credential

_X_API_BASE = "https://api.twitter.com/2"


def _headers() -> dict:
    token = get_credential("X_BEARER_TOKEN")
    if not token:
        raise ValueError("X_BEARER_TOKEN not found in keyring or environment. Add it via set_secret.")
    return {"Authorization": f"Bearer {token}"}


@tool(tags=["social", "x", "twitter"])
def search_x_threads(query: str, max_results: int = 20) -> str:
    """Search X (Twitter) for popular recent tweets matching a query or hashtag.

    Uses X API v2 recent search (last 7 days). Requires X_BEARER_TOKEN env var.
    Results are sorted by engagement (likes + reposts) descending.

    Args:
        query: Search query (e.g. "#AI lang:en", "#MachineLearning", "ChatGPT")
        max_results: Number of tweets to fetch before sorting (10–100, default 20)

    Returns:
        JSON string with tweets including id, conversation_id, author, text,
        created_at, likes, reposts, replies, quotes.
    """
    params = {
        "query": f"{query} -is:retweet lang:en",
        "max_results": min(max(max_results, 10), 100),
        "sort_order": "relevancy",
        "tweet.fields": "created_at,public_metrics,author_id,conversation_id",
        "expansions": "author_id",
        "user.fields": "name,username",
    }
    resp = requests.get(
        f"{_X_API_BASE}/tweets/search/recent",
        headers=_headers(),
        params=params,
        timeout=15,
    )
    if resp.status_code != 200:
        return json.dumps({"error": f"X API HTTP {resp.status_code}", "detail": resp.text})

    data = resp.json()
    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}

    tweets = []
    for t in data.get("data", []):
        author = users.get(t.get("author_id", ""), {})
        m = t.get("public_metrics", {})
        tweets.append({
            "id": t["id"],
            "conversation_id": t.get("conversation_id", t["id"]),
            "author_handle": author.get("username", "unknown"),
            "author_name": author.get("name", ""),
            "text": t["text"],
            "created_at": t.get("created_at", ""),
            "likes": m.get("like_count", 0),
            "reposts": m.get("retweet_count", 0),
            "replies": m.get("reply_count", 0),
            "quotes": m.get("quote_count", 0),
        })

    tweets.sort(key=lambda x: x["likes"] + x["reposts"], reverse=True)
    return json.dumps({"query": query, "count": len(tweets), "tweets": tweets})


@tool(tags=["social", "x", "twitter"])
def get_x_thread_replies(conversation_id: str, max_results: int = 20) -> str:
    """Get top replies for an X (Twitter) thread by conversation ID.

    Fetches all replies in a conversation and sorts by engagement.
    Requires X_BEARER_TOKEN env var.

    Args:
        conversation_id: The conversation/tweet ID from search_x_threads results
        max_results: Number of replies to fetch (10–100, default 20)

    Returns:
        JSON string with replies sorted by engagement, including author,
        text, created_at, likes, reposts, replies counts.
    """
    params = {
        "query": f"conversation_id:{conversation_id} -is:retweet",
        "max_results": min(max(max_results, 10), 100),
        "tweet.fields": "created_at,public_metrics,author_id",
        "expansions": "author_id",
        "user.fields": "name,username",
    }
    resp = requests.get(
        f"{_X_API_BASE}/tweets/search/recent",
        headers=_headers(),
        params=params,
        timeout=15,
    )
    if resp.status_code != 200:
        return json.dumps({"error": f"X API HTTP {resp.status_code}", "detail": resp.text})

    data = resp.json()
    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}

    replies = []
    for t in data.get("data", []):
        author = users.get(t.get("author_id", ""), {})
        m = t.get("public_metrics", {})
        replies.append({
            "id": t["id"],
            "author_handle": author.get("username", "unknown"),
            "author_name": author.get("name", ""),
            "text": t["text"],
            "created_at": t.get("created_at", ""),
            "likes": m.get("like_count", 0),
            "reposts": m.get("retweet_count", 0),
            "replies": m.get("reply_count", 0),
        })

    replies.sort(key=lambda x: x["likes"] + x["reposts"], reverse=True)
    return json.dumps({"conversation_id": conversation_id, "count": len(replies), "replies": replies})
