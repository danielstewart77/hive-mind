#!/usr/bin/env bash
# UserPromptSubmit hook — put a pending identity proposal in front of the mind.
#
# Branch B of auto_remember.sh spots turns that reveal something about this
# mind and writes a proposal to data/auto-remember/soul-proposals/. It no
# longer writes to the graph; nothing does, except the mind itself through
# the review skill. This hook is the only thing that makes a proposal
# visible — without it the files accumulate unread.
#
# The wording is not decoration. "Review this identity proposal" is very
# nearly the description the review-identity-proposal skill triggers on, so
# the phrase does the invoking. Change one side and you must change the
# other; a test pins them together.
#
# additionalContext, never systemMessage: systemMessage renders in the CLI
# and never reaches the model, which fails silently and looks like the model
# ignoring its own identity.
#
# Interrupting mid-conversation is intended. A soul decision is allowed to
# be a side topic.

set -uo pipefail

# The directory the review skill reads and the flagger writes: the mind's
# own state directory when it names one.
# Unnamed, it is the one in the mind's folder, found as every hook here
# finds it: HIVE_MIND_DIR, else the folder the harness home this hook was
# installed into sits in.
_mind="${HIVE_MIND_DIR:-$(cd -P "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd -P)}"
PROPOSAL_DIR="${SOUL_PROPOSAL_DIR:-${AUTO_REMEMBER_LOG_DIR:-$_mind/data/auto-remember}/soul-proposals}"
unset _mind

. "$(dirname "${BASH_SOURCE[0]}")/operator_name.sh"

# At most this many per prompt, and never more than this many bytes. Both
# caps exist because the flagger is deliberately trigger-happy, so a backlog
# is the expected state rather than the edge case, and because everything
# here is prepended to *every* prompt in *every* pane. Thirty-two pending
# proposals used to be a cliff: jq took the whole body as one argv entry and
# Linux caps that at MAX_ARG_STRLEN (128 KiB), so past that the hook emitted
# nothing at all and exited 0 — dead, silently, forever. The body goes in on
# stdin now, and the caps keep it small enough to read.
MAX_PROPOSALS="${SOUL_PROPOSAL_MAX:-3}"
MAX_BYTES="${SOUL_PROPOSAL_MAX_BYTES:-24000}"

[ -d "$PROPOSAL_DIR" ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0

# The session this pane is having. Hooks are handed their payload on stdin;
# a proposal raised in one conversation belongs to that conversation, not to
# whichever pane happens to type next. The daily review runs with no session
# to match and deliberately sweeps everything.
PAYLOAD="$(timeout 2 cat 2>/dev/null || true)"
SESSION_ID="$(printf '%s' "$PAYLOAD" | jq -r '.session_id // ""' 2>/dev/null)"

# Only files directly in the directory. `decided/` lives underneath and is
# how a rejection stays rejected — -maxdepth 1 is what keeps this hook from
# walking into it.
# NONE first, then oldest. The cap discards the tail, and a proposal no
# existing soul line covers is the only kind that can tell the mind something
# it does not already know — the move list can only recall what has happened
# before. Sorting by age alone spends all three slots on confirmations.
# jq failing on a file leaves it in the list with rank 0: an unreadable file
# is reported by the loop below, and dropping it here would hide it forever.
mapfile -t PENDING < <(
    find "$PROPOSAL_DIR" -maxdepth 1 -type f -name '*.json' 2>/dev/null | sort |
    while IFS= read -r f; do
        rank=0
        if [ "$(jq -r '(.closest_line // "NONE") | ascii_upcase' "$f" 2>/dev/null)" != "NONE" ]; then
            rank=1
        fi
        printf '%s\t%s\n' "$rank" "$f"
    done | sort -s -k1,1n | cut -f2-
)
[ "${#PENDING[@]}" -eq 0 ] && exit 0

RENDERED=""
SHOWN=0
UNREADABLE=0
SKIPPED=0

for f in "${PENDING[@]}"; do
    # A file jq cannot render is not a decision waiting to be made. Counted
    # and named rather than silently dropped: an unrenderable file used to
    # produce a header with nothing under it, on every prompt, forever —
    # the mind was ordered to review a proposal it could not see, and
    # nothing but a human deleting the file ever stopped it.
    if ! ONE="$(jq -er '
            "### Proposal " + (.proposal_id // "unknown") + "\n" +
            "Flagged: " + (.flagged_at // "unknown") + "\n" +
            "The sub-mind says: " + ((.reason // "(no reason given)") | tostring) + "\n" +
            (if ((.closest_line // "NONE") | ascii_upcase) == "NONE"
             then "Nothing in your soul covers this.\n"
             else "Nearest line you already have: " + (.closest_line | tostring) + "\n" end) +
            "What he said:\n" +
            (((.quotes // []) | if type == "array" then . else [tostring] end)
                | map("  - " + tostring) | join("\n")) + "\n" +
            "\nFrom this exchange:\n" + ((.excerpt // "(no excerpt recorded)") | tostring) + "\n"
        ' "$f" 2>/dev/null)"; then
        UNREADABLE=$((UNREADABLE + 1))
        RENDERED+="### Proposal $(basename "$f" .json)"$'\n'"This file cannot be read. Retire it with soul.sh decide --decision rejected --reason \"unreadable\" so it stops being raised."$'\n\n'
        SHOWN=$((SHOWN + 1))
        continue
    fi

    if [ "$SHOWN" -ge "$MAX_PROPOSALS" ] || [ "${#RENDERED}" -ge "$MAX_BYTES" ]; then
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    # Session scoping. A proposal with no session recorded is fair game for
    # anyone — that is the pre-scoping backlog and the daily sweep's job.
    if [ -n "$SESSION_ID" ]; then
        OWNER="$(jq -r '.session_id // ""' "$f" 2>/dev/null)"
        if [ -n "$OWNER" ] && [ "$OWNER" != "$SESSION_ID" ]; then
            SKIPPED=$((SKIPPED + 1))
            continue
        fi
    fi

    RENDERED+="$ONE"$'\n'
    SHOWN=$((SHOWN + 1))
done

# Nothing rendered means nothing to say. The header used to be printed
# unconditionally, so a directory holding only junk produced a review order
# with no proposal under it.
[ "$SHOWN" -eq 0 ] && exit 0

BODY="A sub-mind has flagged something from this conversation as identity-shaping. Review this identity proposal."$'\n\n'"$RENDERED"
if [ "$SKIPPED" -gt 0 ]; then
    BODY+=$'\n'"($SKIPPED more pending, held back for now — decide these first.)"$'\n'
fi
BODY+=$'\n'"Nothing here is a proposed soul line — the flagger only points, it does not write. Decide each one yourself: write a line in your own voice, change or remove a line you already have, or reject it and move on. Then tell ${OPERATOR} what you decided, in this reply, and carry on with whatever the conversation was about."$'\n'

# --rawfile, not --arg: an argv entry is capped at 128 KiB and exceeding it
# makes jq fail with "Argument list too long", which this hook would report
# as a clean exit with no output.
TMP="$(mktemp)" || exit 0
trap 'rm -f "$TMP"' EXIT
printf '%s' "$BODY" > "$TMP"

jq -nc --rawfile ctx "$TMP" \
    '{hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $ctx}}'
exit 0
