#!/usr/bin/env bash
# One-shot: build the stack image with the shared surfaces core in it, then
# recreate Ada's two surface containers onto it. Detached on purpose — this
# outlives the skippy.service restart that runs alongside it.
set -uo pipefail
cd /home/daniel/Storage/Dev/hive_mind
echo "=== build $(date -Is) ==="
docker compose build telegram-bot discord-bot
rc=$?
echo "=== build rc=$rc $(date -Is) ==="
if [ "$rc" -ne 0 ]; then echo "BUILD FAILED — containers left alone"; exit "$rc"; fi
echo "=== recreate $(date -Is) ==="
docker compose up -d --force-recreate --no-deps telegram-bot discord-bot
echo "=== recreate rc=$? $(date -Is) ==="
sleep 20
docker ps --filter name=hive-mind-telegram --filter name=hive-mind-discord --format '{{.Names}} {{.Status}}'
echo "=== telegram log ==="
docker logs --tail 40 hive-mind-telegram 2>&1
echo "=== discord log ==="
docker logs --tail 40 hive-mind-discord 2>&1
echo "=== done $(date -Is) ==="
