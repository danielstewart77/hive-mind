#!/usr/bin/env bash
# Install the hive:  curl -fsSL <published address>/install.sh | bash
#
# Checks the machine, clones hive-mind into $HIVE_HOME (default ~/hive), and
# starts the installer detached, so an SSH session that drops does not take it
# down. Prints the setup page's address, port and link. Nothing is written
# before every check has passed, and nothing is written outside $HIVE_HOME.
set -euo pipefail

HIVE_HOME="${HIVE_HOME:-$HOME/hive}"
HIVE_REPO_BASE="${HIVE_REPO_BASE:-https://github.com/danielstewart77}"
MIN_COMPOSE_MAJOR=2
MIN_COMPOSE_MINOR=20
MIN_DISK_GB=40

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
docker info >/dev/null 2>&1 \
  || fail "Docker is not usable without sudo" "sudo usermod -aG docker \$USER, then log out and back in"
compose="$(docker compose version --short 2>/dev/null || true)"
compose="${compose#v}"
major="${compose%%.*}"; rest="${compose#*.}"; minor="${rest%%.*}"
if ! [[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ ]] \
   || (( major < MIN_COMPOSE_MAJOR || (major == MIN_COMPOSE_MAJOR && minor < MIN_COMPOSE_MINOR) )); then
  fail "Docker Compose ${MIN_COMPOSE_MAJOR}.${MIN_COMPOSE_MINOR} or newer is needed (found '${compose:-none}')" \
       "sudo apt-get install -y docker-compose-plugin"
fi
probe="$HIVE_HOME"
while [ ! -e "$probe" ]; do probe="$(dirname "$probe")"; done
free_gb="$(df -Pk "$probe" | awk 'NR==2 {print int($4 / 1048576)}')"
(( free_gb >= MIN_DISK_GB )) \
  || fail "only ${free_gb} GB free under $HIVE_HOME; the hive needs ${MIN_DISK_GB} GB" \
          "free some space, or run with HIVE_HOME=/a/larger/disk/hive"

mkdir -p "$HIVE_HOME/repos" "$HIVE_HOME/state"
chmod 700 "$HIVE_HOME/state"
repo="$HIVE_HOME/repos/hive-mind"
if [ -d "$repo/.git" ]; then
  git -C "$repo" pull --ff-only --quiet
else
  git clone --quiet "$HIVE_REPO_BASE/hive-mind.git" "$repo"
fi

log="$HIVE_HOME/state/installer.log"
mark="$(wc -l < "$log" 2>/dev/null || echo 0)"
cd "$repo"
HIVE_HOME="$HIVE_HOME" HIVE_REPO_BASE="$HIVE_REPO_BASE" \
  setsid nohup python3 -m installer --home "$HIVE_HOME" \
  >>"$HIVE_HOME/state/installer.out" 2>&1 </dev/null &

for _ in $(seq 1 60); do
  url="$(tail -n +"$((mark + 1))" "$log" 2>/dev/null | grep -o 'SETUP_URL .*' | tail -1 | cut -d' ' -f2 || true)"
  if [ -n "$url" ]; then
    port="${url##*:}"; port="${port%%/*}"
    echo "The hive installer is running (it keeps running if this session ends)."
    echo "Setup page: ${url%%:${port}*} port ${port}" | sed 's#http://##'
    echo "  $url"
    echo "If that port is firewalled, from your own machine:"
    echo "  ssh -L ${port}:127.0.0.1:${port} $(whoami)@$(hostname)   then open the link with 127.0.0.1"
    exit 0
  fi
  sleep 1
done
echo "hive install: the installer did not start; see $HIVE_HOME/state/installer.out" >&2
exit 1
