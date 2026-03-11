#!/usr/bin/env python3
"""Search X (Twitter) for tweets and thread replies.

Standalone stateless tool. Dependencies: requests.
"""

import argparse
import json
import os
import sys

# Allow importing core.secrets
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

_X_API_BASE = "https://api.twitter.com/2"

_MOCK_TWEETS = [
    {
        "id": "1001",
        "conversation_id": "1001",
        "author_handle": "alice_ai",
        "author_name": "Alice AI",
        "text": "The future of #AI is here!",
        "created_at": "2026-03-10T12:00:00Z",
        "likes": 150,
        "reposts": 45,
        "replies": 12,
        "quotes": 5,
    },
    {
        "id": "1002",
        "conversation_id": "1002",
        "author_handle": "bob_ml",
        "author_name": "Bob ML",
        "text": "Machine learning trends for 2026 #AI",
        "created_at": "2026-03-10T11:00:00Z",
        "likes": 80,
        "reposts": 20,
        "replies": 5,
        "quotes": 2,
    },
]

_MOCK_REPLIES = [
    {
        "id": "2001",
        "author_handle": "carol_dev",
        "author_name": "Carol Dev",
        "text": "Great thread!",
        "created_at": "2026-03-10T13:00:00Z",
        "likes": 10,
        "reposts": 2,
        "replies": 1,
    },
    {
        "id": "2002",
        "author_handle": "dave_ml",
        "author_name": "Dave ML",
        "text": "Interesting perspective",
        "created_at": "2026-03-10T14:00:00Z",
        "likes": 5,
        "reposts": 1,
        "replies": 0,
    },
]


def _get_bearer_token() -> str:
    from core.secrets import get_credential
    token = get_credential("X_BEARER_TOKEN")
    if not token:
        raise ValueError("X_BEARER_TOKEN not found in keyring or environment. Add it via set_secret.")
    return token


def cmd_search(args: argparse.Namespace) -> int:
    if args.test_mode:
        tweets = sorted(_MOCK_TWEETS, key=lambda x: x["likes"] + x["reposts"], reverse=True)
        print(json.dumps({"query": args.query, "count": len(tweets), "tweets": tweets}))
        return 0

    try:
        import requests
        token = _get_bearer_token()
        params = {
            "query": f"{args.query} -is:retweet lang:en",
            "max_results": min(max(args.max_results, 10), 100),
            "sort_order": "relevancy",
            "tweet.fields": "created_at,public_metrics,author_id,conversation_id",
            "expansions": "author_id",
            "user.fields": "name,username",
        }
        resp = requests.get(
            f"{_X_API_BASE}/tweets/search/recent",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=15,
        )
        if resp.status_code != 200:
            print(json.dumps({"error": f"X API HTTP {resp.status_code}", "detail": resp.text}))
            return 1

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
        print(json.dumps({"query": args.query, "count": len(tweets), "tweets": tweets}))
        return 0
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1


def cmd_replies(args: argparse.Namespace) -> int:
    if args.test_mode:
        replies = sorted(_MOCK_REPLIES, key=lambda x: x["likes"] + x["reposts"], reverse=True)
        print(json.dumps({"conversation_id": args.conversation_id, "count": len(replies), "replies": replies}))
        return 0

    try:
        import requests
        token = _get_bearer_token()
        params = {
            "query": f"conversation_id:{args.conversation_id} -is:retweet",
            "max_results": min(max(args.max_results, 10), 100),
            "tweet.fields": "created_at,public_metrics,author_id",
            "expansions": "author_id",
            "user.fields": "name,username",
        }
        resp = requests.get(
            f"{_X_API_BASE}/tweets/search/recent",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=15,
        )
        if resp.status_code != 200:
            print(json.dumps({"error": f"X API HTTP {resp.status_code}", "detail": resp.text}))
            return 1

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
        print(json.dumps({"conversation_id": args.conversation_id, "count": len(replies), "replies": replies}))
        return 0
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="X (Twitter) API tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="Search for tweets")
    search_parser.add_argument("--query", required=True, help="Search query")
    search_parser.add_argument("--max-results", type=int, default=20, help="Max results (10-100)")
    search_parser.add_argument("--test-mode", action="store_true", help="Use mock data")

    replies_parser = subparsers.add_parser("replies", help="Get thread replies")
    replies_parser.add_argument("--conversation-id", required=True, help="Conversation/tweet ID")
    replies_parser.add_argument("--max-results", type=int, default=20, help="Max results (10-100)")
    replies_parser.add_argument("--test-mode", action="store_true", help="Use mock data")

    args = parser.parse_args()

    if args.command == "search":
        return cmd_search(args)
    elif args.command == "replies":
        return cmd_replies(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
