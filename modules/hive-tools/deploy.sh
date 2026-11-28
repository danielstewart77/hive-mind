#!/usr/bin/env bash
# Build and (re)start the tools service alone, from hive-mind's own checkout.
#
#   modules/hive-tools/deploy.sh            # build, then recreate hive-tools
#   modules/hive-tools/deploy.sh build      # build only
# Run it on the host as the operator, never from a mind.
#
# What it does, and why:
# - The image is built from an export of the COMMITTED tree (git archive of
#   HEAD for this folder and for modules/hive-data), never from the working
#   files. Minds that mount the dev root can edit files in a checkout; they
#   cannot make the next build run those edits unless someone commits them.
#   Uncommitted changes under either folder are named and left out, and a
#   commit no upstream has is refused (see below).
# - The tools' secrets and state are read from HIVE_TOOLS_STATE_DIR (default
#   ~/.config/hive-tools: `.env`, `credentials/`, `data/`), outside the
#   checkout, so no container that mounts the checkout can read them.
# - Only the hive-tools service is touched; the project name is pinned in
#   docker-compose.yml, so this is the same project the running one belongs to.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(git -C "$here" rev-parse --show-toplevel)"
state="${HIVE_TOOLS_STATE_DIR:-$HOME/.config/hive-tools}"
mkdir -p "$state" && state="$(cd "$state" && pwd)"
action="${1:-up}"

[ ! -d "$state/.env" ] || { echo "deploy: $state/.env is a directory (Docker made it when a container mounted it first); remove it and put the tools' .env file there" >&2; exit 2; }
[ -f "$state/.env" ] || { echo "deploy: no $state/.env (the tools' own settings; see .env.example). Carry the old install's .env, credentials/ and data/ into $state, never into the checkout." >&2; exit 2; }
[ -d "$state/credentials" ] || echo "deploy: no $state/credentials yet (Google sign-in files); creating it empty" >&2
[ -d "$state/credentials" ] || install -d -m 700 "$state/credentials"
[ -d "$state/data" ] || echo "deploy: no $state/data yet (the token store); creating it empty. If this tools service ran before, carry its data/ over first" >&2
[ -d "$state/data" ] || install -d -m 700 "$state/data"

dirty="$(git -C "$root" status --porcelain -- modules/hive-tools modules/hive-data)"
if [ -n "$dirty" ]; then
  echo "deploy: uncommitted changes are NOT built:" >&2
  echo "$dirty" >&2
fi

# The committed tree is only as trustworthy as the commit. A mind that mounts
# the checkout writable could commit to it, so a commit the remote does not
# have is not built unless the operator says so on purpose
# (HIVE_TOOLS_BUILD_UNPUSHED=1). The remote ref is fetched first, so a
# locally moved remote-tracking ref proves nothing.
if [ "${HIVE_TOOLS_BUILD_UNPUSHED:-0}" != 1 ]; then
  git -C "$root" fetch --quiet \
    || { echo "deploy: could not fetch the remote to check HEAD against it (HIVE_TOOLS_BUILD_UNPUSHED=1 builds anyway)" >&2; exit 3; }
  git -C "$root" rev-parse --verify -q '@{u}' >/dev/null \
    && git -C "$root" merge-base --is-ancestor HEAD '@{u}' \
    || { echo "deploy: HEAD is not on this branch's upstream; push it (or set HIVE_TOOLS_BUILD_UNPUSHED=1 to build it anyway)" >&2; exit 3; }
fi

# Everything the build and the run read - the compose files too - comes from
# the committed export: a working-tree edit to them (a new mount, privileged)
# is not applied. Laid out as in the checkout (hive-tools beside hive-data) so
# the compose file's relative ../hive-data holds.
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir "$stage/hive-tools" "$stage/hive-data"
git -C "$root" archive HEAD modules/hive-tools | tar -x -C "$stage/hive-tools" --strip-components=2
git -C "$root" archive HEAD modules/hive-data | tar -x -C "$stage/hive-data" --strip-components=2
echo "deploy: building hive-tools from $(git -C "$root" rev-parse --short HEAD)"

export HIVE_TOOLS_STATE_DIR="$state"
here="$stage/hive-tools"
compose=(docker compose --project-directory "$here" --env-file "$state/.env"
         -f "$here/docker-compose.yml" -f "$here/docker-compose.mind-renames.yml")

"${compose[@]}" build hive-tools
[ "$action" = build ] || "${compose[@]}" up -d --no-deps hive-tools
