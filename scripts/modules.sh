#!/usr/bin/env bash
# Clone every module in hive-mind's manifest (.gitmodules) and bring each to
# the latest.
#
#   scripts/modules.sh            # from anywhere inside a hive-mind checkout
#   scripts/modules.sh /path/to/hive-mind
#
# hive-mind runs the newest commit of each module's branch, not the commit
# hive-mind happens to record. A module with `branch = <name>` is fetched and
# fast-forwarded to origin/<name>, and left checked out ON that branch, so work
# in it can be committed and pushed like any other clone. A module that is
# specifically pinned carries `pin = <commit>` in its entry and is checked out
# at that commit instead. An entry naming neither is refused: nothing is pinned
# by accident.
#
# A module checked out on some other branch is somebody's work in progress and
# is left exactly as it is. A module that cannot be brought up — the clone or
# fetch fails, or its branch has diverged from origin — is named, the rest are
# still done, and the script exits non-zero.
set -uo pipefail

root="${1:-$(git rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$root" ] && [ -f "$root/.gitmodules" ] || {
  echo "modules: no .gitmodules at ${root:-this directory}" >&2; exit 2; }
cd "$root" || exit 2

entries() { git config -f .gitmodules --get-regexp '^submodule\..*\.path$'; }

git submodule sync --quiet
failed=0
while read -r key path; do
  name="${key#submodule.}"; name="${name%.path}"
  # The installer's HIVE_REPO_BASE (a mirror or a fork) moves every module
  # with it, as it moves hive-mind itself; the manifest's URLs are the default.
  if [ -n "${HIVE_REPO_BASE:-}" ]; then
    url="${HIVE_REPO_BASE%/}/$(basename "$path").git"
    git config "submodule.$name.url" "$url"
    [ -e "$path/.git" ] && git -C "$path" remote set-url origin "$url"
  fi
  # Only a module not cloned yet is initialised: `submodule update` on one
  # that is would move it off whatever branch it is on.
  if [ ! -e "$path/.git" ]; then
    git submodule update --init --quiet -- "$path" \
      || { echo "$path: could not be cloned" >&2; failed=1; continue; }
  fi
done < <(entries)

while read -r key path; do
  name="${key#submodule.}"; name="${name%.path}"
  [ -e "$path/.git" ] || continue
  branch="$(git config -f .gitmodules --get "submodule.$name.branch" || true)"
  pin="$(git config -f .gitmodules --get "submodule.$name.pin" || true)"
  m=(git -C "$path")
  if [ -n "$pin" ]; then
    if "${m[@]}" fetch --quiet origin && "${m[@]}" checkout --quiet --detach "$pin"; then
      echo "$path: pinned at $("${m[@]}" rev-parse --short HEAD)"
    else
      echo "$path: could not check out its pin $pin" >&2; failed=1
    fi
    continue
  fi
  if [ -z "$branch" ]; then
    echo "$path: its manifest entry names no branch and no pin" >&2; failed=1; continue
  fi
  if ! "${m[@]}" fetch --quiet origin "$branch"; then
    echo "$path: could not fetch $branch" >&2; failed=1; continue
  fi
  current="$("${m[@]}" symbolic-ref --quiet --short HEAD || true)"
  if [ -n "$current" ] && [ "$current" != "$branch" ]; then
    echo "$path: left on $current (work in progress), $branch not updated"
    continue
  fi
  if [ -z "$current" ]; then
    if "${m[@]}" show-ref --verify --quiet "refs/heads/$branch"; then
      "${m[@]}" checkout --quiet "$branch" || { echo "$path: could not check out $branch" >&2; failed=1; continue; }
    else
      "${m[@]}" checkout --quiet -b "$branch" --track "origin/$branch" \
        || { echo "$path: could not check out $branch" >&2; failed=1; continue; }
    fi
  fi
  if ! "${m[@]}" merge --quiet --ff-only "origin/$branch" >/dev/null 2>&1; then
    echo "$path: $branch has diverged from origin/$branch or has conflicting changes; not updated" >&2
    failed=1; continue
  fi
  echo "$path: $branch at $("${m[@]}" rev-parse --short HEAD)"
done < <(entries)

exit "$failed"
