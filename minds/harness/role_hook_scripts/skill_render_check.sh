#!/usr/bin/env bash
# Stop hook — render this mind's skill and agent references into every
# harness, merging an edit made in place under this one into the reference
# first. Detached and silent: the pass must never hold the turn, and its JSON
# report is not hook output.
#
# The installer registers this with the interpreter and the repository it
# was run from as its two arguments, so nothing here names a path.
PYTHON="${1:-python3}"
APP_ROOT="${2:-${HIVE_PROJECT_DIR:-}}"
[ -n "$APP_ROOT" ] || exit 0
setsid "$PYTHON" "$APP_ROOT/tools/stateless/skill_render/skill_render.py" check \
    </dev/null >/dev/null 2>&1 &
exit 0
