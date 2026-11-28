#!/usr/bin/env bash
# Install the hive:  curl -fsSL <published address>/install.sh | bash
#
# Checks the machine, clones hive-mind into $HIVE_HOME (default ~/hive), and
# starts the installer detached, so an SSH session that drops does not take it
# down. Prints the setup page's address, port and link. HIVE_RELEASE=<tag or
# commit> puts the clone on that release (detached); without it a fresh clone is
# the default branch's tip at that moment and an existing one stays where it is
# if it is on a release, else is fast-forwarded. Nothing is written
# before every check has passed, and nothing is written outside $HIVE_HOME.
#
# Everything is inside main(), called on the last line: a download cut short
# by the network runs nothing rather than half a script.
set -euo pipefail

main() {
  HIVE_HOME="${HIVE_HOME:-$HOME/hive}"
  HIVE_REPO_BASE="${HIVE_REPO_BASE:-https://github.com/danielstewart77}"
  local min_major=2 min_minor=20 min_disk_gb=40

  fail() {
    echo "hive install: $1" >&2
    echo "  fix: $2" >&2
    exit 1
  }

  # A module that has become ordinary tracked files of hive-mind (hive-data;
  # the others follow) cannot be pulled over while a git clone of it sits at
  # the same path: git refuses, naming the files it would overwrite. So before
  # the pull, once the update is fetched and shown to carry the module as
  # files, a clone that holds nothing unsaved is moved beside itself (same
  # folder, so its link back to hive-mind's .git still resolves) and left for
  # the operator to delete. A clone with uncommitted work, or commits no
  # remote has, is never moved: the install stops and says so.
  make_room_for_moved_modules() {
    local repo="$1" name path stamp
    for name in hive-data hive-surfaces hive terminal hive-tools; do
      path="modules/$name"
      [ -e "$repo/$path/.git" ] || continue
      git -C "$repo" ls-tree HEAD -- "$path" | grep -q '^160000 ' || continue
      git -C "$repo" fetch --quiet \
        || fail "could not fetch $repo" "check the network (and any web proxy), then run again"
      git -C "$repo" rev-parse --verify -q '@{u}' >/dev/null \
        || fail "$repo has no upstream branch, so the update cannot be read" \
                "git -C $repo branch --set-upstream-to origin/<branch>, then run again"
      git -C "$repo" ls-tree '@{u}' -- "$path" | grep -q '^040000 ' || continue
      [ -z "$(git -C "$repo/$path" status --porcelain)" ] \
        || fail "$repo/$path has uncommitted changes, and $name now lives inside hive-mind" \
                "commit or discard them there (or copy them into $repo/$path after the update), then run again"
      [ -z "$(git -C "$repo/$path" log --branches HEAD --not --remotes --oneline)" ] \
        && [ -z "$(git -C "$repo/$path" stash list)" ] \
        || fail "$repo/$path has commits that no remote has, and $name now lives inside hive-mind" \
                "git -C $repo/$path push, or keep them elsewhere, then run again"
      stamp="$(date +%Y%m%d%H%M%S)"
      mv "$repo/$path" "$repo/modules/.$name.moved-aside-$stamp" \
        || fail "could not move $repo/$path aside" "move it away by hand, then run again"
      echo "$name is now part of hive-mind: the old clone was moved to $repo/modules/.$name.moved-aside-$stamp (safe to delete once you have carried its untracked .env and data/ into modules/$name; hive-tools is the exception: its .env, credentials/ and data/ go to ~/.config/hive-tools, never into the checkout)."
    done
  }

  release_is_not_a_branch() {
    case "$2" in origin/*|refs/heads/*|refs/remotes/*) fail "$2 is a branch, and a release is a tag or a commit" "name a tag or a commit" ;; esac
    git -C "$1" show-ref --verify -q "refs/remotes/origin/$2" 2>/dev/null \
      && fail "$2 is a branch, and a release is a tag or a commit" "name a tag (git ls-remote --tags $HIVE_REPO_BASE/hive-mind.git) or a commit"
    return 0
  }

  command -v git >/dev/null 2>&1 || fail "git is not installed" "sudo apt-get install -y git"
  command -v python3 >/dev/null 2>&1 || fail "python3 is not installed" "sudo apt-get install -y python3"
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || fail "Python 3.10 or newer is needed" "install a newer python3 from your distribution"
  command -v docker >/dev/null 2>&1 \
    || fail "Docker is not installed or not on PATH" "https://docs.docker.com/engine/install/ then: sudo usermod -aG docker \$USER"
  local docker_root
  docker_root="$(timeout 30 docker info --format '{{.DockerRootDir}}' 2>/dev/null)" \
    || fail "Docker is not usable without sudo (or did not answer in 30s)" "sudo usermod -aG docker \$USER, then log out and back in"
  local compose major rest minor
  compose="$(timeout 30 docker compose version --short 2>/dev/null || true)"
  compose="${compose#v}"
  major="${compose%%.*}"; rest="${compose#*.}"; minor="${rest%%.*}"
  if ! [[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ ]] \
     || (( major < min_major || (major == min_major && minor < min_minor) )); then
    fail "Docker Compose ${min_major}.${min_minor} or newer is needed (found '${compose:-none}')" \
         "sudo apt-get install -y docker-compose-plugin"
  fi
  timeout 30 docker buildx version >/dev/null 2>&1 \
    || fail "Docker buildx is not installed (the images are built with BuildKit)" \
            "sudo apt-get install -y docker-buildx-plugin"

  # Free space where the images land (Docker's data folder) and where the
  # install folder goes.
  local where probe free_gb
  for where in "${docker_root:-/var/lib/docker}" "$HIVE_HOME"; do
    probe="$where"
    while [ ! -e "$probe" ]; do probe="$(dirname "$probe")"; done
    free_gb="$(df -Pk "$probe" 2>/dev/null | awk 'NR==2 {print int($4 / 1048576)}')"
    [ -n "$free_gb" ] || continue
    (( free_gb >= min_disk_gb )) \
      || fail "only ${free_gb} GB free under $where; the hive needs ${min_disk_gb} GB" \
              "free some space, or run with HIVE_HOME=/a/larger/disk/hive"
  done

  # An installer already running for this folder: its page is asked to start
  # its run again, which it does only when that run failed; a run going or
  # waiting on a question is left alone. Either way its link is reprinted.
  if [ -f "$HIVE_HOME/state/page.lock" ] && ! python3 - "$HIVE_HOME/state/page.lock" <<'PY'
import fcntl, sys
with open(sys.argv[1], "a") as fh:
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(1)
PY
  then
    local link answer
    link="$(cat "$HIVE_HOME/state/page.link" 2>/dev/null || true)"
    answer="$(python3 - "$link" <<'PY' || true
import json, sys, urllib.error, urllib.request
from urllib.parse import parse_qs, urlparse
link = urlparse(sys.argv[1])
code = (parse_qs(link.query).get("code") or [""])[0]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def call(method, path):
    req = urllib.request.Request(f"http://127.0.0.1:{link.port}{path}",
                                 data=b"{}" if method == "POST" else None, method=method,
                                 headers={"X-Setup-Code": code,
                                          "Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except Exception:
        return 0, {}

status, _ = call("POST", "/api/restart")
if status == 200:
    print("restarted")
elif status == 409:
    print("busy")
else:
    # A page from before /api/restart existed: what its run is doing
    # decides whether it is left alone or has to be stopped once.
    status, state = call("GET", "/api/state")
    failed = status == 200 and not state.get("running") and \
        bool(state.get("failed_step") or state.get("error"))
    print("busy" if status == 200 and not failed else "unanswered")
PY
)"
    case "$answer" in
      restarted)
        echo "The hive installer's last run here failed; it is running again on its page:"
        echo "  $link"
        echo "(It runs the installer code it started with. For a newer installer, stop it:"
        echo "  pkill -f 'python3 -m installer --home $HIVE_HOME'   then run this command again.)"
        exit 0 ;;
      busy)
        echo "The hive installer is already open here. Its page:"
        echo "  $link"
        exit 0 ;;
    esac
    echo "hive install: an installer is already open here but cannot start its failed run" >&2
    echo "  again (it runs older code, or is not answering). Its page: ${link:-(none recorded)}" >&2
    echo "  fix: stop it, then run this command again:" >&2
    echo "    pkill -f 'python3 -m installer --home $HIVE_HOME'" >&2
    exit 1
  fi

  mkdir -p "$HIVE_HOME/repos" "$HIVE_HOME/state"
  chmod 700 "$HIVE_HOME/state"
  local repo="$HIVE_HOME/repos/hive-mind"
  if [ -d "$repo/.git" ]; then
    make_room_for_moved_modules "$repo"
    if [ -n "${HIVE_RELEASE:-}" ]; then
      # A named release (a tag or a commit), never a branch tip.
      release_is_not_a_branch "$repo" "$HIVE_RELEASE"
      git -C "$repo" fetch --quiet --tags origin \
        && git -C "$repo" checkout --quiet --detach "$HIVE_RELEASE" \
        || fail "could not put $repo on release $HIVE_RELEASE" "git -C $repo status — commit or discard local changes, check the name, then run again"
    elif git -C "$repo" symbolic-ref --quiet HEAD >/dev/null; then
      git -C "$repo" pull --ff-only --quiet \
        || fail "could not update $repo" "git -C $repo status — commit or discard local changes, then run again"
    fi
    # else: on a release already (scripts/release.sh put it there); it stays.
  else
    rm -rf "$repo.partial"   # left by a clone the network cut short
    git clone --quiet "$HIVE_REPO_BASE/hive-mind.git" "$repo.partial" \
      || fail "could not clone hive-mind from $HIVE_REPO_BASE" "check the network (and any web proxy), then run again"
    if [ -n "${HIVE_RELEASE:-}" ]; then
      release_is_not_a_branch "$repo.partial" "$HIVE_RELEASE"
      git -C "$repo.partial" checkout --quiet --detach "$HIVE_RELEASE" \
        || fail "$HIVE_RELEASE is not a tag or commit of hive-mind" "name one: git ls-remote --tags $HIVE_REPO_BASE/hive-mind.git"
    fi
    mv "$repo.partial" "$repo"
  fi

  local log="$HIVE_HOME/state/installer.log" mark=0
  [ -f "$log" ] && mark="$(wc -l < "$log")"
  cd "$repo"
  HIVE_HOME="$HIVE_HOME" HIVE_REPO_BASE="$HIVE_REPO_BASE" HIVE_RELEASE="${HIVE_RELEASE:-}" \
    setsid nohup python3 -m installer --home "$HIVE_HOME" \
    >>"$HIVE_HOME/state/installer.out" 2>&1 </dev/null &

  local url port
  for _ in $(seq 1 60); do
    url="$( { tail -n +"$((mark + 1))" "$log" 2>/dev/null || true; } | grep -o 'SETUP_URL .*' | tail -1 | cut -d' ' -f2 || true)"
    if [ -n "$url" ]; then
      port="${url##*:}"; port="${port%%/*}"
      echo "The hive installer is running (it keeps running if this session ends)."
      echo "Setup page: port ${port}"
      echo "  $url"
      echo "If that port is firewalled, from your own machine:"
      echo "  ssh -L ${port}:127.0.0.1:${port} $(whoami)@$(hostname)   then open the link with 127.0.0.1"
      exit 0
    fi
    sleep 1
  done
  echo "hive install: the installer did not start; see $HIVE_HOME/state/installer.out" >&2
  exit 1
}

main "$@"
