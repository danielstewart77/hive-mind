#!/usr/bin/env bash
# Put this machine's hive-mind checkout on a named release, and go back.
#
#   scripts/release.sh [options] to <tag-or-commit>    # onto that release
#   scripts/release.sh [options] back                   # onto the one before
#   scripts/release.sh [options] status                 # where it stands
#
# A release is a commit of hive-mind, named by a tag or by the commit itself;
# never by a branch. `to` checks the tree out DETACHED at it, brings the three
# modules kept in their own repositories to the commits that tree records
# (scripts/modules.sh), writes where it came from to the state directory,
# writes the release the console builds new minds from (see below), and
# rebuilds the images from that tree. `back` is `to` the release recorded as
# the previous one; running it again goes forward again.
#
# What it never does: write a .env file, a generated compose fragment, a
# docker-compose.yml, a named volume or a data folder; run a database
# migration (a release that went back leaves the database as the newer one
# changed it, and the older data package refuses to START a migration check
# against a step it does not know, so the migration step is skipped on going
# back and is the operator's on going forward); recreate a container (add
# --restart to do it). It refuses, naming why, and changes nothing, when:
# tracked files of the checkout have uncommitted changes; the ref names no
# commit, or one that no remote publishes; the state directory is inside the
# checkout; an old clone at a path hive-mind now holds as files has unsaved
# work; or going to an older release while any mind has been moved to its one
# key (it must be put back first: `tools/stateless/key_migration` undo).
#
# Options
#   --state-dir D    where it keeps the record of releases (default $HIVE_HOME/state);
#                    never inside the checkout
#   --minds-dir D    the minds folder the console reads (default $HIVE_HOME/minds when
#                    HIVE_HOME is set, else <checkout>/minds)
#   --compose F      the stack's compose file, repeatable (default $HIVE_HOME/docker-compose.yml
#                    when HIVE_HOME is set, else <checkout>/docker-compose.yml)
#   --env-file F     passed to the stack's `docker compose` (default $HIVE_HOME/.env, if HIVE_HOME)
#   --no-build       do not rebuild images
#   --restart        after building, recreate the stack's containers (`up -d`)
#   --no-record      do not write a release record of what is running before it moves
#   --minds-checked  you have run the key-migration report yourself and no mind is moved
#                    (for when the console is down; skips the check, nothing else)
#   --dry-run        say what it would do and change nothing
#
# Environment (for tests and unusual hosts): HIVE_RELEASE_DOCKER (the docker
# command), HIVE_RELEASE_NO_FETCH=1, HIVE_RELEASE_CONTEXT_URL (the address a
# console-built mind fetches the release from; default: origin),
# HIVE_RELEASE_MINDS_REPORT (a command printing the key-migration report as
# JSON; default tools/stateless/key_migration/key_migration.py report, which
# needs HIVE_SITE_URL and HIVE_OPERATOR_KEY).
set -uo pipefail

die() { echo "release: $1" >&2; [ -n "${2:-}" ] && echo "  fix: $2" >&2; exit "${3:-1}"; }

state_dir="${HIVE_RELEASE_STATE_DIR:-${HIVE_HOME:+$HIVE_HOME/state}}"
minds_dir="${HIVE_HOME:+$HIVE_HOME/minds}"
env_file="${HIVE_HOME:+$HIVE_HOME/.env}"
composes=()
build=1; restart=0; record=1; dry=0; minds_checked=0
while [ $# -gt 0 ]; do
  case "$1" in
    --state-dir|--minds-dir|--compose|--env-file)
      [ $# -ge 2 ] && [ -n "$2" ] || die "$1 needs a value" ;;&
    --state-dir) state_dir="$2"; shift 2 ;;
    --minds-dir) minds_dir="$2"; shift 2 ;;
    --compose)   composes+=("$2"); shift 2 ;;
    --env-file)  env_file="$2"; shift 2 ;;
    --no-build)  build=0; shift ;;
    --restart)   restart=1; shift ;;
    --no-record) record=0; shift ;;
    --minds-checked) minds_checked=1; shift ;;
    --dry-run)   dry=1; shift ;;
    -*) die "unknown option $1" ;;
    *) break ;;
  esac
done
command="${1:-}"; target_ref="${2:-}"
case "$command" in
  to) [ -n "$target_ref" ] || die "to needs a tag or a commit" ;;
  back|status) ;;
  *) die "usage: release.sh [options] to <tag-or-commit> | back | status" ;;
esac

root="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null)" \
  || die "this script is not inside a hive-mind checkout"
cd "$root" || exit 2
root="$(pwd -P)"
docker_cmd="${HIVE_RELEASE_DOCKER:-docker}"
[ -n "$minds_dir" ] || minds_dir="$root/minds"
if [ ${#composes[@]} -eq 0 ] && [ -n "${HIVE_HOME:-}" ]; then composes=("$HIVE_HOME/docker-compose.yml"); fi
if [ ${#composes[@]} -eq 0 ]; then composes=("$root/docker-compose.yml"); fi

[ -n "$state_dir" ] || die "no state directory" "pass --state-dir (or set HIVE_HOME): where the record of releases is kept, outside the checkout"
mkdir -p "$state_dir/release" 2>/dev/null || die "cannot create $state_dir/release"
state_dir="$(cd "$state_dir" && pwd -P)"
case "$state_dir/" in "$root"/*) die "the state directory $state_dir is inside the checkout" "keep it outside, for example beside the checkout" ;; esac
rel="$state_dir/release"

head_commit="$(git rev-parse HEAD)"
describe() { git describe --tags --exact-match "$1" 2>/dev/null || echo "$1"; }

if [ "$command" = "status" ]; then
  echo "current:  $head_commit ($(describe "$head_commit"))"
  [ -f "$rel/previous" ] && echo "previous: $(cat "$rel/previous")" || echo "previous: none recorded"
  git symbolic-ref -q HEAD >/dev/null && echo "note: the checkout is on a branch ($(git symbolic-ref --short HEAD)), not a release"
  exit 0
fi

# ---- what release? ---------------------------------------------------------
if [ "$command" = "back" ]; then
  [ -s "$rel/previous" ] || die "no previous release is recorded in $rel" "name the release: release.sh to <tag-or-commit>"
  target_ref="$(awk '{print $1}' "$rel/previous")"
fi
if [ "${HIVE_RELEASE_NO_FETCH:-}" != "1" ]; then
  git fetch --quiet --tags origin 2>/dev/null || die "could not fetch origin" "check the network, then run again (HIVE_RELEASE_NO_FETCH=1 uses what this checkout already has)"
fi
target="$(git rev-parse -q --verify "${target_ref}^{commit}" 2>/dev/null)" \
  || die "$target_ref names no commit of this repository" "use a tag (git tag -l) or a full commit"
case "$target_ref" in
  origin/*|refs/remotes/*) die "$target_ref is a branch; a release is a tag or a commit" ;;
esac
git show-ref --verify -q "refs/remotes/$target_ref" 2>/dev/null && die "$target_ref is a branch; a release is a tag or a commit"
git show-ref --verify -q "refs/heads/$target_ref" 2>/dev/null && die "$target_ref is a branch; a release is a tag or a commit"
if [ -z "$(git branch -r --contains "$target" 2>/dev/null)" ] \
   && ! git ls-remote --tags origin 2>/dev/null | grep -q "^$target"; then
  die "$target is on no remote branch and under no remote tag" "push it first: a mind the console builds is fetched from the remote at that commit"
fi
target_short="${target:0:9}"

# ---- refusals before anything changes -------------------------------------
# A module left on a branch by a developer is not a change to this checkout
# (modules.sh leaves it, and says so).
dirty="$(git status --porcelain --untracked-files=no --ignore-submodules=all)"
if [ -n "$dirty" ]; then
  echo "$dirty" >&2
  die "tracked files have uncommitted changes (listed above)" "commit them, or git stash, then run again"
fi

older=0
if [ "$head_commit" != "$target" ] && ! git merge-base --is-ancestor "$head_commit" "$target"; then older=1; fi
if [ "$older" = 1 ] && [ "$minds_checked" = 0 ]; then
  report_cmd="${HIVE_RELEASE_MINDS_REPORT:-}"
  if [ -n "$report_cmd" ]; then report="$($report_cmd 2>/dev/null)"; rc=$?
  else report="$(python3 tools/stateless/key_migration/key_migration.py report 2>/dev/null)"; rc=$?; fi
  moved="$(printf '%s' "$report" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    print(" ".join(m["mind"] for m in data["minds"] if m.get("state") == "moved"))
except Exception:
    print("?UNREADABLE")
')"
  if [ "$rc" -ne 0 ] || [ "$moved" = "?UNREADABLE" ]; then
    die "going to an older release needs to know no mind has been moved to its one key, and the console's report could not be read" \
        "set HIVE_SITE_URL and HIVE_OPERATOR_KEY so key_migration.py report works, or run it yourself and put every moved mind back first"
  fi
  [ -z "$moved" ] || die "these minds have been moved to their one key: $moved. The release you are going to does not know that key" \
      "put each back first: python3 tools/stateless/key_migration/key_migration.py undo <mind> (not possible after the old keys were retired: then going back no longer holds for those minds)"
fi

# An old clone sitting where hive-mind now holds files cannot be checked out over.
moved_names=(hive-data hive-surfaces hive terminal hive-tools)
for name in "${moved_names[@]}"; do
  path="modules/$name"
  [ -e "$root/$path/.git" ] || continue
  git ls-tree "$target" -- "$path" | grep -q '^040000 ' || continue
  [ -z "$(git -C "$root/$path" status --porcelain)" ] \
    || die "$path is an old clone with uncommitted changes, and $name now lives inside hive-mind" "commit or discard them there, then run again"
  [ -z "$(git -C "$root/$path" log --branches HEAD --not --remotes --oneline)" ] && [ -z "$(git -C "$root/$path" stash list)" ] \
    || die "$path is an old clone with commits no remote has" "git -C $path push, or keep them elsewhere, then run again"
done

if [ "$dry" = 1 ]; then
  echo "would put the checkout on $target ($(describe "$target")), from $head_commit"
  echo "would write $rel/previous, $rel/current and $minds_dir/.release-context"
  [ "$build" = 1 ] && echo "would rebuild the images from that tree"
  exit 0
fi

# ---- record what is running, then move -------------------------------------
if [ "$record" = 1 ] && [ -x "$(command -v "$docker_cmd")" ] && [ -f tools/stateless/release_record/release_record.py ]; then
  mkdir -p "$state_dir/release-records"
  python3 tools/stateless/release_record/release_record.py --read-installed-commits \
    --out "$state_dir/release-records/$(date +%Y%m%d%H%M%S)-before-$target_short.json" >/dev/null 2>&1 \
    || echo "release: no release record of the running state could be written (continuing)" >&2
fi

if [ "$head_commit" != "$target" ]; then
  for name in "${moved_names[@]}"; do
    path="modules/$name"
    [ -e "$root/$path/.git" ] || continue
    git ls-tree "$target" -- "$path" | grep -q '^040000 ' || continue
    aside="modules/.$name.moved-aside-$(date +%Y%m%d%H%M%S)"
    mv "$root/$path" "$root/$aside" || die "could not move $path aside" "move it away by hand, then run again"
    echo "release: $name is part of hive-mind now; the old clone was moved to $aside (carry its untracked .env and data/ into $path, then delete it; hive-tools keeps those in ~/.config/hive-tools)"
  done
  git checkout --quiet --detach "$target" || die "could not check out $target_short" "git status shows why"
  printf '%s %s\n' "$head_commit" "$(describe "$head_commit")" > "$rel/previous"
fi
printf '%s %s\n' "$target" "$(describe "$target")" > "$rel/current"
printf '%s %s %s -> %s\n' "$(date -u +%FT%TZ)" "$command" "$head_commit" "$target" >> "$rel/history.log"

if [ -f .gitmodules ] && [ -f scripts/modules.sh ]; then
  bash scripts/modules.sh "$root" || echo "release: some modules were not brought to the release's commits (listed above); the checkout is on the release" >&2
fi

# The release the console builds a new mind from: this commit, at the address
# it is published at. Written beside the minds it creates; nothing here is a
# secret. A tag cannot name its own commit, so this is read from the checkout.
url="$(printf '%s' "${HIVE_RELEASE_CONTEXT_URL:-$(git remote get-url origin 2>/dev/null)}" | sed -E 's#^(https?://)[^/]*@#\1#')"
case "$url" in
  https://*|http://*)
    mkdir -p "$minds_dir" 2>/dev/null || true
    if [ -d "$minds_dir" ]; then printf '%s#%s\n' "$url" "$target" > "$minds_dir/.release-context"
    else echo "release: cannot create $minds_dir, so the console's release file was not written" >&2; fi ;;
  *) rm -f "$minds_dir/.release-context"
     echo "release: origin ($url) is not an http(s) address a console build can fetch from, so the console's release file was not written; set HIVE_RELEASE_CONTEXT_URL (the checkout is on the release; only new minds built by the console are affected)" >&2 ;;
esac

echo "release: checkout is on $target ($(describe "$target")); previous recorded as $(cut -d' ' -f1 "$rel/previous" 2>/dev/null | cut -c1-9)"

# ---- rebuild ---------------------------------------------------------------
fail=0
build_stack() {  # build_stack <label> <docker compose args...>
  local label="$1"; shift
  echo "release: building $label"
  "$docker_cmd" compose "$@" build || { echo "release: the build of $label failed" >&2; fail=1; return; }
  if [ "$restart" = 1 ]; then "$docker_cmd" compose "$@" up -d || { echo "release: could not recreate $label" >&2; fail=1; }; fi
}
if [ "$build" = 1 ]; then
  for f in "${composes[@]}"; do
    if [ ! -f "$f" ]; then echo "release: $f does not exist; not built" >&2; fail=1; continue; fi
    args=(-f "$f"); [ -n "$env_file" ] && [ -f "$env_file" ] && args+=(--env-file "$env_file")
    [ -n "${HIVE_HOME:-}" ] && args=(-p hive "${args[@]}" --project-directory "$HIVE_HOME")
    build_stack "$f" "${args[@]}"
  done
  # The console and the terminal are their own projects; built here only on a
  # machine where each has the .env that marks it as run from its folder.
  for site in hive terminal; do
    if [ -f "modules/$site/.env" ] && [ -f "modules/$site/docker-compose.yml" ]; then
      build_stack "modules/$site" -f "modules/$site/docker-compose.yml"
    fi
  done
  if [ -x modules/hive-tools/deploy.sh ] && [ -f "${HIVE_TOOLS_STATE_DIR:-$HOME/.config/hive-tools}/.env" ]; then
    echo "release: building hive-tools"
    if [ "$restart" = 1 ]; then bash modules/hive-tools/deploy.sh up; else bash modules/hive-tools/deploy.sh build; fi || { echo "release: the build of hive-tools failed" >&2; fail=1; }
  fi
  [ "$restart" = 1 ] || echo "release: images rebuilt, containers not recreated: bind-mounted source is already the new tree; recreate to load it (add --restart, or docker compose up -d)"
fi
[ "$command" = "back" ] && echo "release: the database was not touched and no migration was run: the older release works against what the newer one added" || echo "release: if this release carries a database step, run it once (docs/setup/one-repository.md)"
exit "$fail"
