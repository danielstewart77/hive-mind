---
name: x-search
description: Search X (Twitter) for tweets and thread replies
user-invocable: false
---

# X Search Tool

Search X (Twitter) for popular recent tweets or get thread replies.

## Usage

### Search tweets
```bash
/usr/src/app/tools/stateless/x_api/venv/bin/python /usr/src/app/tools/stateless/x_api/x_api.py search \
  --query "<search query>" \
  --max-results 20
```

### Get thread replies
```bash
/usr/src/app/tools/stateless/x_api/venv/bin/python /usr/src/app/tools/stateless/x_api/x_api.py replies \
  --conversation-id "<tweet_id>" \
  --max-results 20
```

## Arguments

### search
- `--query` (required): Search query (e.g. "#AI lang:en", "ChatGPT")
- `--max-results` (optional): Number of tweets (10-100, default 20)

### replies
- `--conversation-id` (required): Tweet/conversation ID from search results
- `--max-results` (optional): Number of replies (10-100, default 20)

## Output

JSON with tweets/replies sorted by engagement (likes + reposts) descending.
