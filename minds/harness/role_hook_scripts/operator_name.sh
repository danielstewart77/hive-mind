# Sourced by the hooks that address the mind's operator by name. Sets
# OPERATOR to the capitalised `owner:` of this mind's runtime.yaml, or to
# "the operator" when there is none. The file is the one the mind server
# names in HIVE_RUNTIME_PATH, else the runtime.yaml of the mind folder this
# hook was installed into (two levels above the hooks directory).
_runtime="${HIVE_RUNTIME_PATH:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)/runtime.yaml}"
OPERATOR="${HIVE_OPERATOR_NAME:-}"
if [ -z "$OPERATOR" ] && [ -r "$_runtime" ]; then
    _owner="$(sed -n 's/^owner:[[:space:]]*//p' "$_runtime" | head -n 1 | tr -d "\"'" | tr -cd '[:alnum:] _.-')"
    [ -n "$_owner" ] && OPERATOR="$(printf '%s' "${_owner:0:1}" | tr '[:lower:]' '[:upper:]')${_owner:1}"
fi
[ -n "$OPERATOR" ] || OPERATOR="the operator"
unset _runtime _owner
