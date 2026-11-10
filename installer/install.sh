#!/usr/bin/env bash
# Install the hive:  curl -fsSL <published address>/install.sh | bash
#
# Checks the machine, clones hive-mind into $HIVE_HOME (default ~/hive), and
# starts the installer detached, so an SSH session that drops does not take it
# down. Prints the setup page's address, port and link. Nothing is written
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

  # An installer already running for this folder: reprint its page.
  if [ -f "$HIVE_HOME/state/page.lock" ] && ! python3 - "$HIVE_HOME/state/page.lock" <<'PY'
import fcntl, sys
with open(sys.argv[1], "a") as fh:
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(1)
PY
  then
    echo "The hive installer is already running here. Its page:"
    echo "  $(cat "$HIVE_HOME/state/page.link" 2>/dev/null || echo '(see the earlier output)')"
    exit 0
  fi

  mkdir -p "$HIVE_HOME/repos" "$HIVE_HOME/state"
  chmod 700 "$HIVE_HOME/state"
  local repo="$HIVE_HOME/repos/hive-mind"
  if [ -d "$repo/.git" ]; then
    git -C "$repo" pull --ff-only --quiet \
      || fail "could not update $repo" "git -C $repo status — commit or discard local changes, then run again"
  else
    git clone --quiet "$HIVE_REPO_BASE/hive-mind.git" "$repo.partial" \
      || fail "could not clone hive-mind from $HIVE_REPO_BASE" "check the network (and any web proxy), then run again"
    mv "$repo.partial" "$repo"
  fi

  local log="$HIVE_HOME/state/installer.log" mark=0
  [ -f "$log" ] && mark="$(wc -l < "$log")"
  cd "$repo"
  HIVE_HOME="$HIVE_HOME" HIVE_REPO_BASE="$HIVE_REPO_BASE" \
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
