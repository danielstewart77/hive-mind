#!/usr/bin/env bash
# /always-remember backend — direct write to lucent at standing tier.
# No classification (the operator deliberately invoked /always-remember; the
# rule belongs in the standing tier by user declaration). Pure bash +
# jq + curl.
#
# Usage:
#   bash always_remember.sh           # this mind only (mind_id from .env)
#   bash always_remember.sh shared    # cross-mind, every mind sees it
#
# Input: rule text on stdin.
# Output (stdout): summary line with entry_id on success, or error.
# Exit: 0 on success, non-zero on failure.

set -uo pipefail

SCOPE="${1:-self}"
case "$SCOPE" in
    self|shared) ;;
    *)
        echo "FAIL: scope must be 'self' or 'shared' (got: $SCOPE)"
        exit 2
        ;;
esac

# The harness inherits this mind's environment from its service unit.
# HIVE_PROJECT_DIR locates repo content; setup.sh stamps it into .env.
PROJECT_DIR="${HIVE_PROJECT_DIR:-}"

LUCENT_URL="${LUCENT_URL_SELF:-http://127.0.0.1:8425}"
LUCENT_AUTH="Authorization: Bearer ${LUCENT_BEARER_TOKEN:-}"
MIND_ID="${MIND_ID:?MIND_ID required in .env}"

# scope=shared writes a sentinel mind_id so bootstrap_loader unions
# it into every mind's standing-rules query.
if [ "$SCOPE" = "shared" ]; then
    TARGET_MIND_ID="shared"
else
    TARGET_MIND_ID="$MIND_ID"
fi

CHUNK=$(cat)
if [ -z "$CHUNK" ]; then
    echo "FAIL: empty input on stdin"
    exit 1
fi

LUCENT_BODY=$(jq -nc \
    --arg c "$CHUNK" \
    --arg a "$TARGET_MIND_ID" \
    '{content: $c, data_class: "feedback", tier: "standing", mind_id: $a, source: "always-remember"}')

LUCENT_RESP=$(curl -sS -m 30 -X POST "$LUCENT_URL/memory/store" \
    -H "$LUCENT_AUTH" \
    -H "Content-Type: application/json" \
    -d "$LUCENT_BODY" 2>&1)
LUCENT_EXIT=$?

ENTRY_ID=$(printf '%s' "$LUCENT_RESP" | jq -r '.id // ""' 2>/dev/null)

if [ $LUCENT_EXIT -ne 0 ] || [ -z "$ENTRY_ID" ] || [ "$ENTRY_ID" = "null" ]; then
    echo "FAIL: lucent write failed (curl exit $LUCENT_EXIT)"
    printf '%s\n' "$LUCENT_RESP"
    exit 1
fi

# Standing-tier count audit (REQ-029 — soft cap of 10).
# Uses the server-side tier filter — single call.
STANDING_COUNT=$(curl -sS -m 10 -H "$LUCENT_AUTH" \
    "$LUCENT_URL/memory/list?tier=standing&limit=1" 2>/dev/null \
    | jq '.total // 0' 2>/dev/null || echo "?")

# Overflow notification (REQ-029): one-shot per cap-cross. State file
# at $LOG_ROOT/standing-cap-warned suppresses repeats. Removed by the
# pruner when count drops back below the cap.
LOG_ROOT="${AUTO_REMEMBER_LOG_DIR:-${PROJECT_DIR:-$HOME}/data/auto-remember}"
WARN_FILE="$LOG_ROOT/standing-cap-warned"
if [ "$STANDING_COUNT" != "?" ] && [ "$STANDING_COUNT" -gt 10 ] && [ ! -f "$WARN_FILE" ]; then
    MSG="Standing tier is at $STANDING_COUNT entries (soft cap: 10). Demote one via lucent UPDATE."
    nohup "$PROJECT_DIR/.venv/bin/python3" \
        "$PROJECT_DIR/tools/stateless/notify/notify.py" \
        send --message "$MSG" \
        --channels telegram,file \
        --alert-file "$LOG_ROOT/alerts.log" \
        > /dev/null 2>&1 &
    disown
    mkdir -p "$LOG_ROOT" 2>/dev/null
    touch "$WARN_FILE"
fi

echo "PASS: tier=standing scope=$SCOPE mind_id=$TARGET_MIND_ID entry_id=$ENTRY_ID"
echo "standing-tier count: $STANDING_COUNT (soft cap: 10)"
exit 0
