#!/usr/bin/env bash
# UserPromptSubmit hook — inject the current date/time on every turn.
#
# Pure bash. No Python, no venv, no dependencies. The harness fires this
# on every user message; output is parsed as JSON and merged into the
# context the model sees for that turn.
#
# The timezone is the install's: HIVE_MIND_TZ when the mind sets one, else
# the TZ the hive's installer stamps on every service from the machine it
# runs on, else whatever the system clock is in. Nothing here names a place.

set -uo pipefail

TZ_VALUE="${HIVE_MIND_TZ:-${TZ:-}}"
if [ -n "$TZ_VALUE" ]; then
    NOW=$(TZ="$TZ_VALUE" date "+%A, %B %-d, %Y at %-I:%M %p %Z")
else
    NOW=$(date "+%A, %B %-d, %Y at %-I:%M %p %Z")
fi

# Must be hookSpecificOutput.additionalContext — a top-level systemMessage
# is UI-only and silently never reaches the model.
printf '{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "Current date and time: %s."}}\n' "$NOW"
exit 0
