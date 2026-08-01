---
name: end-session
description: Harvest durable memory from this session (preferences, current and future configuration), then close the session for good. Use when the user types /end-session or asks to end, kill, or permanently wrap up the current session — unlike /rotate-session, nothing respawns.
user-invocable: true
---

# /end-session

Harvest what this session learned, then terminate it. The gateway marks the session closed (it moves to Archived in the web terminal); no replacement spawns.

1. Harvest. Review the whole conversation and extract every durable fact in three categories: preferences (how the user wants things done), current configuration (settings and facts about systems as they now are), and future configuration (planned or intended changes not yet made). Pipe each fact separately through the remember pipeline:

```bash
cat <<'__REMEMBER_INPUT__' | bash "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/remember/remember.sh"
<one fact, verbatim>
__REMEMBER_INPUT__
```

Skip trivia. An empty category is fine — save nothing rather than padding.

2. Schedule the kill, detached, so this turn's reply is delivered before the process dies:

```bash
SID=$(curl -s -H "Authorization: Bearer $COMMS_BEARER_TOKEN" "$COMMS_URL/sessions" | \
  jq -r --arg c "$CLAUDE_CODE_SESSION_ID" '.[] | select(.claude_sid==$c) | .id' | head -1)
[ -n "$SID" ] && nohup bash -c "sleep 8; curl -s -X DELETE -H \"Authorization: Bearer $COMMS_BEARER_TOKEN\" \"$COMMS_URL/sessions/$SID\"" >/dev/null 2>&1 &
echo "closing gateway session: ${SID:-none found}"
```

If `SID` is empty this claude process is not a gateway session (e.g. a local CLI run) — say so and stop; there is nothing to close.

3. Say goodbye as the final message: one line on what was harvested, and that the session is closing now.
