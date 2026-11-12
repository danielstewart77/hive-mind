#!/usr/bin/env bash
# The mind's own hands on its own soul. Carried inside the skill directory,
# because a skill is a directory and the sync copies all of it — a helper in
# a shared scripts/ folder does not travel, and is how a skill arrives broken.
#
# This exists because the recipe used to be prose. A grill pass followed it
# literally and got a jq error: the block referenced $NEW_SOUL_JSON_ARRAY,
# which nothing defined, and moved files named <id>.json. Instructions that
# read plausibly and do not execute are the exact failure a skill is for —
# the mind meets them cold, once, on the turn that matters.
#
#   soul.sh read
#   soul.sh write   --line "..." [--line "..."] --reason "..." [--allow-shrink]
#   soul.sh decide  <proposal-id> --decision accepted|reworded|rejected \
#                                 --reason "..." [--final "..."]
#
# `write` takes the complete list, because rewrite and removal cannot be
# expressed as an append. Read first, edit, send the whole thing.
#
# The key is the mind's own lucent credential, LUCENT_BEARER_TOKEN — the same
# value its memory hooks present. Lucent answers it for this mind's soul and
# refuses it, 403, for any other. There is no separate soul key on a mind.
set -uo pipefail

PROPOSAL_DIR="${SOUL_PROPOSAL_DIR:-${AUTO_REMEMBER_LOG_DIR:-${HIVE_PROJECT_DIR:-$HOME}/data/auto-remember}/soul-proposals}"
LUCENT="${LUCENT_URL_SELF:-${LUCENT_URL:-}}"
TOKEN="${LUCENT_BEARER_TOKEN:-}"
NAME="${MIND_NAME:-}"
NAME="$(printf '%s' "${NAME^}")"

die() { printf '{"ok":false,"error":"%s"}\n' "$1" >&2; exit 1; }

[ -n "$LUCENT" ] || die "LUCENT_URL_SELF is not set"
[ -n "$NAME" ]   || die "MIND_NAME is not set"
[ -n "$TOKEN" ]  || die "LUCENT_BEARER_TOKEN is not set; it is this mind's own lucent key"

api() {  # method path [body]
    local method="$1" path="$2" body="${3:-}"
    if [ -n "$body" ]; then
        curl -sS -m 30 -X "$method" "$LUCENT$path" \
            -H "Authorization: Bearer $TOKEN" \
            -H "Content-Type: application/json" -d "$body"
    else
        curl -sS -m 30 -X "$method" "$LUCENT$path" \
            -H "Authorization: Bearer $TOKEN"
    fi
}

read_soul() {
    local body
    body="$(api GET "/graph/soul?name=$(printf '%s' "$NAME" | jq -sRr @uri)")"
    # A read that failed must not look like an empty soul: the caller builds
    # the whole list from this, so "" and [] both mean "delete everything I
    # am" by the time the write goes out.
    printf '%s' "$body" | jq -e '.ok == true' >/dev/null 2>&1 \
        || die "could not read the soul: $(printf '%s' "$body" | head -c 300)"
    printf '%s\n' "$body"
}

case "${1:-}" in
read)
    read_soul
    ;;

write)
    shift
    LINES='[]'; REASON=''; SHRINK=false
    while [ $# -gt 0 ]; do
        case "$1" in
            --line)  LINES="$(printf '%s' "$LINES" | jq -c --arg v "$2" '. + [$v]')"; shift 2 ;;
            --reason) REASON="$2"; shift 2 ;;
            --allow-shrink) SHRINK=true; shift ;;
            *) die "unknown argument: $1" ;;
        esac
    done
    [ -n "$REASON" ] || die "--reason is required; it is stored with the change"

    CURRENT="$(read_soul)" || exit 1
    SEEN="$(printf '%s' "$CURRENT" | jq -r '.change_count')"

    # expected_change_count is the compare-and-set. Another pane, or the
    # console, may have written between the read above and this line; without
    # it the loser's edit vanishes and both writers report success.
    RESP="$(api POST /graph/soul "$(jq -nc \
        --arg name "$NAME" --argjson soul "$LINES" --arg reason "$REASON" \
        --argjson seen "$SEEN" --argjson shrink "$SHRINK" \
        '{name:$name, soul_values:$soul, actor:"mind", reason:$reason,
          expected_change_count:$seen, allow_shrink:$shrink}')")"

    printf '%s\n' "$RESP"
    printf '%s' "$RESP" | jq -e '.ok == true' >/dev/null 2>&1 || exit 1
    ;;

decide)
    shift
    PID="${1:-}"; shift || true
    [ -n "$PID" ] || die "which proposal?"
    DECISION=''; FINAL=''; REASON=''
    while [ $# -gt 0 ]; do
        case "$1" in
            --decision) DECISION="$2"; shift 2 ;;
            --final)    FINAL="$2"; shift 2 ;;
            --reason)   REASON="$2"; shift 2 ;;
            *) die "unknown argument: $1" ;;
        esac
    done
    case "$DECISION" in
        accepted|reworded|rejected) ;;
        *) die "--decision must be accepted, reworded or rejected" ;;
    esac
    [ -n "$REASON" ] || die "--reason is required"

    SRC="$PROPOSAL_DIR/$PID.json"
    [ -f "$SRC" ] || die "no pending proposal $PID"
    mkdir -p "$PROPOSAL_DIR/decided"

    # Move last, and only on a decision that actually happened. A proposal in
    # decided/ after a failed write is a change reported as made that nobody
    # made — and it is never raised again, so nothing brings it back.
    jq --arg d "$DECISION" --arg f "$FINAL" --arg w "$REASON" \
       --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
       '. + {decision:$d, final_wording:$f, reasoning:$w, decided_at:$at}' \
       "$SRC" > "$PROPOSAL_DIR/decided/$PID.json.partial" \
        && mv "$PROPOSAL_DIR/decided/$PID.json.partial" "$PROPOSAL_DIR/decided/$PID.json" \
        && rm -f "$SRC" \
        || die "could not record the decision for $PID"

    printf '{"ok":true,"proposal_id":"%s","decision":"%s"}\n' "$PID" "$DECISION"
    ;;

*)
    die "usage: soul.sh read | write --line ... --reason ... | decide <id> --decision ... --reason ..."
    ;;
esac
