#!/usr/bin/env bash
# Rebuild onto the core that reads Discord's keyring key, recreate every
# surface, and bring Cypher's up for the first time now his token is stored.
set -uo pipefail
cd /home/daniel/Storage/Dev/hive_mind
SURFACES="telegram-bot discord-bot bob-bot nagatha-bot bilby-bot cypher-bot"
echo "=== build $(date -Is) ==="
docker compose build $SURFACES
rc=$?
echo "=== build rc=$rc $(date -Is) ==="
[ "$rc" -ne 0 ] && { echo "BUILD FAILED — containers left alone"; exit "$rc"; }
echo "=== recreate $(date -Is) ==="
docker compose up -d --force-recreate --no-deps $SURFACES
echo "=== recreate rc=$? $(date -Is) ==="
sleep 30
echo "=== status ==="
for c in hive-mind-telegram hive-mind-discord hive-mind-bob-bot hive-mind-nagatha-bot hive-mind-bilby-bot hive-mind-cypher-bot; do
  printf '%s :: %s\n' "$c" "$(docker inspect -f '{{.State.Status}} restarts={{.RestartCount}}' "$c" 2>&1)"
done
echo "=== refusals, if any ==="
for c in hive-mind-telegram hive-mind-discord hive-mind-bob-bot hive-mind-nagatha-bot hive-mind-bilby-bot hive-mind-cypher-bot; do
  hits=$(docker logs --tail 40 "$c" 2>&1 | grep -ciE "not found in|not set|Traceback|Unauthorized" || true)
  printf '%s :: %s suspicious line(s)\n' "$c" "$hits"
done
echo "=== done $(date -Is) ==="
