#!/usr/bin/env bash
# Build the stack image with the shared surfaces core, then recreate every
# surface container onto it. Run as its own transient unit, not merely
# setsid: a plain setsid child still sits in skippy.service's cgroup, and
# systemd's default KillMode takes the whole cgroup down on restart — which
# is what killed the first attempt at 06:41 mid-COPY.
#
# cypher-bot is deliberately not started: its token is not in the keyring
# yet, and `restart: unless-stopped` would crashloop it.
set -uo pipefail
cd /home/daniel/Storage/Dev/hive_mind
SURFACES="telegram-bot discord-bot bob-bot nagatha-bot bilby-bot"
echo "=== build $(date -Is) ==="
docker compose build $SURFACES
rc=$?
echo "=== build rc=$rc $(date -Is) ==="
[ "$rc" -ne 0 ] && { echo "BUILD FAILED — containers left alone"; exit "$rc"; }
echo "=== recreate $(date -Is) ==="
docker compose up -d --force-recreate --no-deps $SURFACES
echo "=== recreate rc=$? $(date -Is) ==="
sleep 25
echo "=== status ==="
docker ps -a --filter name=hive-mind-telegram --filter name=hive-mind-discord \
  --filter name=hive-mind-bob-bot --filter name=hive-mind-nagatha-bot \
  --filter name=hive-mind-bilby-bot --format '{{.Names}} :: {{.Status}}'
for c in hive-mind-telegram hive-mind-discord hive-mind-bob-bot hive-mind-nagatha-bot hive-mind-bilby-bot; do
  echo "=== log $c ==="
  docker logs --tail 25 "$c" 2>&1
done
echo "=== done $(date -Is) ==="
