#!/usr/bin/env bash
# Clone the modules hive-mind keeps in their own repositories (inference-proxy,
# auth-service, hive-harness-dsh: the manifest, .gitmodules) and bring each to
# the exact commit this checkout of hive-mind records for it.
#
#   scripts/modules.sh            # from anywhere inside a hive-mind checkout
#   scripts/modules.sh /path/to/hive-mind
#
# A release of hive-mind names one commit of each module: the one recorded in
# its tree (a gitlink). That is what runs, so that the same release always
# builds the same code and going back a release goes back for the modules too.
# A module is checked out DETACHED at that commit. A module already there is
# left alone.
#
# Work is never moved or dropped. A module checked out on a branch is left
# exactly as it is (and named, with the commit the release records). One sitting
# detached on commits that no remote has, or with uncommitted changes, is left
# as it is, named, and the script exits non-zero. A module that cannot be
# brought up (the clone or fetch fails) is named, the rest are still done, and
# the script exits non-zero.
#
# Following a branch tip is the explicit opt-in for development:
#
#   HIVE_MODULES_FLOAT=1 scripts/modules.sh
#
# fetches each module's `branch = <name>` (from the manifest) and fast-forwards
# it, leaving it ON that branch so work can be committed and pushed; an entry
# carrying `pin = <commit>` is held at that commit instead. Nothing in the
# repository sets it, and the installer never does.
#
# The five repositories that became ordinary folders of hive-mind (hive-data,
# hive-surfaces, hive, terminal, hive-tools) are not in the manifest and are
# not touched here. An old clone of one of them at its path is made room for by
# installer/install.sh and `scripts/release.sh` before the tree that holds it as
# files is checked out.
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


if [ "${HIVE_MODULES_FLOAT:-}" != "1" ]; then
  while read -r key path; do
    [ -e "$path/.git" ] && checked_out "$path" || continue
    target="$(git rev-parse -q --verify "HEAD:$path" 2>/dev/null || true)"
    if [ -z "$target" ]; then
      echo "$path: this checkout records no commit for it" >&2; failed=1; continue
    fi
    m=(git -C "$path")
    short="${target:0:7}"
    current="$("${m[@]}" symbolic-ref --quiet --short HEAD || true)"
    if [ "$("${m[@]}" rev-parse HEAD)" = "$target" ]; then
      echo "$path: at $short, the release's commit"
      continue
    fi
    if [ -n "$current" ]; then
      echo "$path: left on $current (work in progress); the release records $short"
      continue
    fi
    if [ -n "$("${m[@]}" status --porcelain --untracked-files=no)" ]; then
      echo "$path: has uncommitted changes; left as it is (the release records $short)" >&2
      failed=1; continue
    fi
    if ! "${m[@]}" cat-file -e "$target^{commit}" 2>/dev/null; then
      "${m[@]}" fetch --quiet origin 2>/dev/null || true
      "${m[@]}" cat-file -e "$target^{commit}" 2>/dev/null \
        || { echo "$path: the commit $short the release records is not in its repository" >&2
             failed=1; continue; }
    fi
    # Moving off the commit it sits on loses nothing only if some remote has
    # it (or the release's commit contains it).
    if ! "${m[@]}" merge-base --is-ancestor HEAD "$target" \
       && [ -z "$("${m[@]}" branch -r --contains HEAD 2>/dev/null)" ]; then
      "${m[@]}" fetch --quiet origin 2>/dev/null || true
      if [ -z "$("${m[@]}" branch -r --contains HEAD 2>/dev/null)" ]; then
        echo "$path: detached on commits no remote has; left as it is (the release records $short)" >&2
        failed=1; continue
      fi
    fi
    if "${m[@]}" checkout --quiet --detach "$target"; then
      echo "$path: at $short, the release's commit"
    else
      echo "$path: could not check out $short" >&2; failed=1
    fi
  done < <(entries)
  exit "$failed"
fi
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
