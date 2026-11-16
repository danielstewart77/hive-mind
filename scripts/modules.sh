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
# Work is never moved or dropped. A module checked out on another branch is
# left exactly as it is, pin or not; so is one sitting detached on commits its
# branch does not have. A module that cannot be brought up — the clone or
# fetch fails, its branch has diverged, or it holds commits it would lose — is
# named, the rest are still done, and the script exits non-zero.
#
# A module already cloned keeps its own `origin`: an operator's fork or SSH
# address is theirs. HIVE_REPO_BASE (a mirror or fork of every repository)
# applies to the modules this run clones.
set -uo pipefail

root="${1:-$(git rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$root" ] && [ -f "$root/.gitmodules" ] || {
  echo "modules: no .gitmodules at ${root:-this directory}" >&2; exit 2; }
cd "$root" || exit 2

entries() { git config -f .gitmodules --get-regexp '^submodule\..*\.path$'; }
checked_out() { [ -n "$(ls -A "$1" 2>/dev/null | grep -vx '\.git')" ]; }

clone_git=(git)
case "${HIVE_REPO_BASE:-}" in
  /*|file://*) clone_git=(git -c protocol.file.allow=always) ;;  # a local mirror
esac

failed=0
while read -r key path; do
  name="${key#submodule.}"; name="${name%.path}"
  [ -e "$path/.git" ] && checked_out "$path" && continue
  if [ -n "${HIVE_REPO_BASE:-}" ]; then
    git config "submodule.$name.url" "${HIVE_REPO_BASE%/}/$(basename "$path").git"
  fi
  # Only a module not cloned yet, or one whose clone was cut short before its
  # files were written, is (re)initialised: `submodule update` on one already
  # checked out would move it off whatever it is on.
  "${clone_git[@]}" submodule update --init --force --quiet -- "$path" \
    && checked_out "$path" \
    || { echo "$path: could not be cloned" >&2; failed=1; }
done < <(entries)

while read -r key path; do
  name="${key#submodule.}"; name="${name%.path}"
  [ -e "$path/.git" ] && checked_out "$path" || continue
  branch="$(git config -f .gitmodules --get "submodule.$name.branch" || true)"
  pin="$(git config -f .gitmodules --get "submodule.$name.pin" || true)"
  m=(git -C "$path")
  current="$("${m[@]}" symbolic-ref --quiet --short HEAD || true)"
  if [ -n "$current" ] && [ "$current" != "$branch" ]; then
    echo "$path: left on $current (work in progress)"
    continue
  fi
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
  if [ -z "$current" ]; then
    # Detached: what `git submodule update` leaves. Moving onto the branch
    # is safe only if no commit here would be left behind.
    if ! "${m[@]}" merge-base --is-ancestor HEAD "origin/$branch"; then
      echo "$path: detached on commits origin/$branch does not have; left as it is" >&2
      failed=1; continue
    fi
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
