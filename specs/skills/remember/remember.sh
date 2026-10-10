#!/usr/bin/env bash
# /remember backend — classify content via hive-tools /ollama/structured,
# then POST to lucent /memory/store. Pure bash + jq + curl, mirrors the
# capture branch of the per-turn auto-remember hook.
#
# Ships inside the skill directory rather than in a shared scripts/ dir:
# a skill is a directory and the sync copies all of it, so a mind that
# installs this skill gets a working one.
#
# Input: chunk text on stdin.
# Output (stdout): one-line summary + entry_id on success, or error message.
# Exit: 0 on success, non-zero on failure.

set -uo pipefail

# The harness inherits this mind's environment from its service unit, so
# there is nothing to source. HIVE_PROJECT_DIR is the one path this needs:
# where this mind's checkout lives, since the data-class specs the
# classifier reads are repo content.
PROJECT_DIR="${HIVE_PROJECT_DIR:-}"
if [ -z "$PROJECT_DIR" ] || [ ! -d "$PROJECT_DIR/specs/data-classes" ]; then
    echo "FAIL: HIVE_PROJECT_DIR is unset or has no specs/data-classes. setup.sh stamps it into .env."
    exit 1
fi

LUCENT_URL="${LUCENT_URL_SELF:-http://127.0.0.1:8425}"
LUCENT_AUTH="Authorization: Bearer ${LUCENT_BEARER_TOKEN:-}"
HIVE_TOOLS_URL="${HIVE_TOOLS_URL:-http://127.0.0.1:9421}"
HIVE_TOOLS_TOKEN="${HIVE_TOOLS_TOKEN:-}"
SPECS_DIR="$PROJECT_DIR/specs/data-classes"
LOG_ROOT="${AUTO_REMEMBER_LOG_DIR:-$PROJECT_DIR/data/auto-remember}"
MIND_ID="${MIND_ID:?MIND_ID required in .env}"

CHUNK=$(cat)
if [ -z "$CHUNK" ]; then
    echo "FAIL: empty input on stdin"
    exit 1
fi

if [ -z "$HIVE_TOOLS_TOKEN" ]; then
    echo "FAIL: HIVE_TOOLS_TOKEN not in env (.env not sourced or token missing)"
    exit 1
fi

TS=$(date +%Y%m%dT%H%M%S)
NS=$(date +%N | head -c 6)
RUN_DIR="$LOG_ROOT/runs/${TS}-${NS}-remember"
mkdir -p "$RUN_DIR"
printf '%s' "$CHUNK" > "$RUN_DIR/input.md"

# Build data_class enum + spec text for the prompt. Skip the index.
# All four current classes (ephemeral, current-state, future-state,
# feedback) are valid auto-classifier targets; the standing tier is
# guarded server-side by the source field, not by class.
CLASS_NAMES_JSON=$(jq -nc '[]')
SPEC_TEXT=""
for spec in "$SPECS_DIR"/*.md; do
    [ -f "$spec" ] || continue
    name=$(basename "$spec" .md)
    case "$name" in
        index) continue ;;
    esac
    body=$(cat "$spec")
    CLASS_NAMES_JSON=$(jq -nc --argjson acc "$CLASS_NAMES_JSON" --arg n "$name" \
        '$acc + [$n]')
    SPEC_TEXT=$(printf '%s\n\n## %s\n%s' "$SPEC_TEXT" "$name" "$body")
done

SCHEMA_JSON=$(jq -nc --argjson classes "$CLASS_NAMES_JSON" '
    {
        type: "object",
        properties: {
            data_class: { type: "string", enum: $classes },
            reason:     { type: "string" },
            action:     { type: "string", enum: ["save-vector","save-graph","notify","discard"] }
        },
        required: ["data_class","reason","action"],
        additionalProperties: false
    }')

PROMPT=$(printf 'Classify the following content against the data class specs below. Pick the single best-fit class. If no class fits, return action="discard" and pick whichever class is closest as data_class.\n\n### Content\n%s\n\n### Specs%s' "$CHUNK" "$SPEC_TEXT")

REQ_BODY=$(jq -nc --arg p "$PROMPT" --argjson s "$SCHEMA_JSON" \
    '{prompt: $p, schema: $s}')
printf '%s' "$REQ_BODY" > "$RUN_DIR/ollama-request.json"

OLLAMA_RESP=$(curl -sS -m 90 -X POST "$HIVE_TOOLS_URL/ollama/structured" \
    -H "Authorization: Bearer $HIVE_TOOLS_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$REQ_BODY" 2>&1)
OLLAMA_EXIT=$?
printf '%s' "$OLLAMA_RESP" > "$RUN_DIR/ollama-response.json"

if [ $OLLAMA_EXIT -ne 0 ]; then
    echo "FAIL: Ollama call failed (curl exit $OLLAMA_EXIT). Run dir: $RUN_DIR"
    exit 1
fi

DATA_CLASS=$(printf '%s' "$OLLAMA_RESP" | jq -r '.data_class // ""' 2>/dev/null)
ACTION=$(printf '%s' "$OLLAMA_RESP" | jq -r '.action // ""' 2>/dev/null)
REASON=$(printf '%s' "$OLLAMA_RESP" | jq -r '.reason // ""' 2>/dev/null)

if [ "$ACTION" != "save-vector" ] || [ -z "$DATA_CLASS" ] || [ "$DATA_CLASS" = "null" ]; then
    rm -rf "$RUN_DIR"
    echo "DISCARD: classifier returned action=$ACTION class=$DATA_CLASS reason=$REASON"
    exit 0
fi

LUCENT_BODY=$(jq -nc \
    --arg c "$CHUNK" \
    --arg d "$DATA_CLASS" \
    --arg a "$MIND_ID" \
    '{content: $c, data_class: $d, tier: "contextual", mind_id: $a, source: "user"}')
printf '%s' "$LUCENT_BODY" > "$RUN_DIR/lucent-request.json"

LUCENT_RESP=$(curl -sS -m 30 -X POST "$LUCENT_URL/memory/store" \
    -H "$LUCENT_AUTH" \
    -H "Content-Type: application/json" \
    -d "$LUCENT_BODY" 2>&1)
LUCENT_EXIT=$?
printf '%s' "$LUCENT_RESP" > "$RUN_DIR/lucent-response.json"

ENTRY_ID=$(printf '%s' "$LUCENT_RESP" | jq -r '.id // ""' 2>/dev/null)

if [ $LUCENT_EXIT -ne 0 ] || [ -z "$ENTRY_ID" ] || [ "$ENTRY_ID" = "null" ]; then
    echo "FAIL: lucent write failed (curl exit $LUCENT_EXIT). Run dir: $RUN_DIR"
    cat "$RUN_DIR/lucent-response.json"
    exit 1
fi

# Success: clean up breadcrumb dir, report
rm -rf "$RUN_DIR"
echo "PASS: data_class=$DATA_CLASS action=save-vector entry_id=$ENTRY_ID"
echo "reason: $REASON"
exit 0
