#!/usr/bin/env bash
# UserPromptSubmit hook — name the surface this turn arrived on.
#
# HIVE_SURFACE is stamped on the harness process at spawn time: hive-comms
# derives it from the session's surface for stream-json spawns (telegram,
# discord, mind, scheduler, ...), spawn_pty hardcodes "terminal" for the web
# terminal's tmux pane, and a shell profile can export "local" for a harness the
# operator starts by hand. Unset means a spawn that predates the stamping
# (deploy window) — say nothing rather than guess. Behavior rules keyed off
# the surface (TTS prose for telegram, etc.) live in their own hooks; this
# one only states the fact.

set -uo pipefail

SURFACE="${HIVE_SURFACE:-}"
[ -z "$SURFACE" ] && exit 0

printf '{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "<message-surface>%s</message-surface> This turn arrived via the %s surface. Shape your reply for how it will be consumed there."}}\n' "$SURFACE" "$SURFACE"
exit 0
