#!/usr/bin/env bash
# UserPromptSubmit hook: remind the model to respond in TTS-friendly prose.
# A spoken surface reads replies aloud, so markdown lists, code fences in
# conversational text, forward slashes as separators, and arrow characters
# all break the audio.
#
# The rule is for spoken surfaces only. A terminal (web tile or local shell)
# renders text, so the reminder is skipped there. Unset HIVE_SURFACE means a
# spawn that predates surface stamping — assume spoken, the pre-surface
# status quo, so a spoken surface never loses the rule during a deploy window.

case "${HIVE_SURFACE:-}" in
  terminal|local) exit 0 ;;
esac

. "$(dirname "${BASH_SOURCE[0]}")/operator_name.sh"

printf '{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "Respond in plain prose. The reply is read aloud by TTS to %s, so anything that does not pronounce cleanly is a bug. Do not use numbered lists, bullet points, dashes as bullets, forward slashes as separators, arrow characters, or code fences in conversational replies. Quote file paths, function names, and commands inline naturally. Code edits and tool calls themselves are fine because they are not spoken; this rule governs only the prose you write back to %s. Keep it as concise as possible. Try to stay around two short paragraphs at most, though that is a guide and not a hard limit. Do not reiterate, do not restate, do not summarize what you already said. Say the thing once and be done."}}\n' "$OPERATOR" "$OPERATOR"
