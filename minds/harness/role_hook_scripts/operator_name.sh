# Sourced by the hooks that address the mind's operator by name. Sets
# OPERATOR to the capitalised `owner:` of this mind's runtime.yaml, or to
# "the operator" when there is none. The file is the one the mind server
# names in HIVE_RUNTIME_PATH, else the runtime.yaml of the mind's folder:
# HIVE_MIND_DIR when the mind names it, else the folder the harness home
# this hook was installed into sits in (two levels above this file).
_runtime="${HIVE_RUNTIME_PATH:-${HIVE_MIND_DIR:-$(cd -P "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd -P)}/runtime.yaml}"
# The name goes into JSON between quotes, so it carries none of its own.
OPERATOR="$(printf '%s' "${HIVE_OPERATOR_NAME:-}" | tr -d "\"'\\\\" | tr -d '[:cntrl:]')"
if [ -z "$OPERATOR" ] && [ -r "$_runtime" ]; then
    _owner="$(sed -n 's/^owner:[[:space:]]*//p' "$_runtime" | head -n 1 | sed 's/^#.*$//; s/[[:space:]]#.*$//; s/[[:space:]]*$//' | tr -d "\"'\\\\" | tr -d '[:cntrl:]')"
    case "$_owner" in null|Null|NULL|"~") _owner="" ;; esac
    [ -n "$_owner" ] && OPERATOR="$(printf '%s' "${_owner:0:1}" | tr '[:lower:]' '[:upper:]')${_owner:1}"
fi
[ -n "$OPERATOR" ] || OPERATOR="the operator"
unset _runtime _owner
