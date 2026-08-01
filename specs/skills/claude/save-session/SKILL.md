---
name: save-session
description: Harvest durable memory from the session so far without closing it. Use when the user says "save session", "save this session", or "/save-session" — unlike /end-session, the conversation continues.
tools: Bash
user-invocable: true
---

# /save-session

The harvest half of `/end-session`, with nothing killed afterward. Use it
when a session has produced something worth keeping but is not finished.

Review the whole conversation and extract every durable fact in three
categories:

- **preferences** — how the user wants things done
- **current configuration** — settings and facts about systems as they now are
- **future configuration** — planned or intended changes not yet made

Pipe each fact separately through the remember pipeline, one invocation per
fact:

```bash
cat <<'__REMEMBER_INPUT__' | bash "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/remember/remember.sh"
<one fact, verbatim>
__REMEMBER_INPUT__
```

Skip trivia. An empty category is fine — save nothing rather than padding,
since a corpus of unremarkable facts is worse than a small true one.

A fact the classifier discards is not a failure. It returns
`DISCARD: ... reason=<r>`, which is it doing its job.

Report in one line what landed and what was discarded, then continue the
conversation where it left off.
