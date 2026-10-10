#!/usr/bin/env bash
# Put every install of hive-surfaces on this host onto the same commit of main,
# then report the commit each one is actually running.
#
# Every mind runs the one package, but each install bakes in whatever commit
# was current when it was last built — so "upgrade the bots" was a list of
# places to remember, and one of them always got forgotten. This finds them
# instead: any running container with the package installed, the bare-metal
# venvs named below, and the remote boxes named below.
#
# Containers are rebuilt and recreated one at a time, and only when behind.
# A bare-metal mind is reinstalled but never restarted: that process may be
# the one carrying the conversation that ran this script.
set -uo pipefail

REPO=https://github.com/danielstewart77/hive-surfaces.git
BARE_METAL=(
  "skippy|/home/daniel/Storage/hive-edge-mind-skippy|skippy.service"
)
REMOTE=(
  "arnold|daniel@192.168.5.64"
  "zack|daniel@192.168.5.62"
)

PROBE='import importlib.metadata as m, json
try:
    print(json.loads(m.distribution("hive-surfaces").read_text("direct_url.json"))["vcs_info"]["commit_id"])
except Exception:
    pass'

target=$(git ls-remote "$REPO" refs/heads/main | cut -f1)
[ -n "$target" ] || { echo "cannot read hive-surfaces main — nothing touched"; exit 1; }
echo "target: ${target:0:7}"

container_commit() {
  docker exec "$1" sh -c "for p in /opt/venv/bin/python /app/.venv/bin/python python3; do
    out=\$(\$p -c '$PROBE' 2>/dev/null); [ -n \"\$out\" ] && { echo \"\$out\"; exit; }; done" 2>/dev/null
}

declare -A REPORT
failed=0

# Containers, grouped by the compose service that builds them.
for c in $(docker ps --format '{{.Names}}'); do
  have=$(container_commit "$c")
  [ -n "$have" ] || continue
  if [ "$have" = "$target" ]; then
    REPORT[$c]="${have:0:7} current"
    continue
  fi
  wd=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$c")
  files=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$c")
  svc=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$c")
  if [ -z "$svc" ]; then
    REPORT[$c]="${have:0:7} BEHIND — not a compose service, upgrade by hand"
    failed=1; continue
  fi
  args=(--project-directory "$wd")
  IFS=',' read -ra fl <<< "$files"
  for f in "${fl[@]}"; do args+=(-f "$f"); done
  echo "== $c (${have:0:7}) — building $svc"
  if ! docker compose "${args[@]}" build "$svc" >"/tmp/surface-build-$c.log" 2>&1; then
    REPORT[$c]="${have:0:7} BEHIND — build failed, see /tmp/surface-build-$c.log"
    failed=1; continue
  fi
  docker compose "${args[@]}" up -d --force-recreate --no-deps "$svc" >/dev/null 2>&1
  sleep 5
  now=$(container_commit "$c")
  if [ "$now" = "$target" ]; then
    REPORT[$c]="${now:0:7} upgraded"
  else
    REPORT[$c]="${now:0:7} BEHIND after rebuild"
    failed=1
  fi
done

# Bare metal: reinstall, never restart.
for entry in "${BARE_METAL[@]}"; do
  IFS='|' read -r name dir unit <<< "$entry"
  py="$dir/.venv/bin/python"
  have=$("$py" -c "$PROBE" 2>/dev/null)
  if [ "$have" != "$target" ]; then
    (cd "$dir" && bash scripts/upgrade-surfaces.sh >/tmp/surface-build-$name.log 2>&1)
    have=$("$py" -c "$PROBE" 2>/dev/null)
  fi
  if [ "$have" != "$target" ]; then
    REPORT[$name]="${have:0:7} BEHIND — reinstall failed, see /tmp/surface-build-$name.log"
    failed=1
  else
    started=$(systemctl show "$unit" -p ActiveEnterTimestampMonotonic --value)
    installed=$(stat -c %Y "$("$py" -c 'import hive_surfaces,os;print(os.path.dirname(hive_surfaces.__file__))')")
    boot=$(awk '{print int($1)}' /proc/uptime); now_s=$(date +%s)
    started_epoch=$(( now_s - boot + started / 1000000 ))
    if [ "$installed" -gt "$started_epoch" ]; then
      REPORT[$name]="${have:0:7} installed — running process is older, restart $unit"
    else
      REPORT[$name]="${have:0:7} current"
    fi
  fi
done

# Remote boxes: report only. A box that is off is said to be off.
for entry in "${REMOTE[@]}"; do
  IFS='|' read -r name host <<< "$entry"
  if ! timeout 10 ssh -o ConnectTimeout=5 -o BatchMode=yes "$host" true 2>/dev/null; then
    REPORT[$name]="unreachable — not checked"
    failed=1; continue
  fi
  REPORT[$name]="reachable — check by hand (Windows install, not automated)"
  failed=1
done

echo
echo "=== every surface install, against ${target:0:7} ==="
for k in $(printf '%s\n' "${!REPORT[@]}" | sort); do
  printf '%-28s %s\n' "$k" "${REPORT[$k]}"
done
exit $failed
