#!/usr/bin/env bash
# Story 1.2.20 (was 1.2.19): comms and lucent move onto the one database.
#
# Run detached, because the restart in the middle of it ends every live
# conversation — including the one that started it:
#
#     PRE_SHA=711e0ef setsid nohup scripts/cutover-comms-lucent.sh >/dev/null 2>&1 &
#
# PRE_SHA is the hive_mind commit whose comms and lucent source the running
# containers loaded; the way back checks that source out under the old images.
# The build installs whatever branch the hive-data checkout has out, so that
# checkout must be on main with the comms operations merged.
#
# Everything it does is written to $LOG, and the outcome to $LOG.result as one
# word (switched, refused, reverted, failed-check) so whoever reads it next does
# not have to parse the whole log to know where the hive is.
#
# The order is the one the story fixes:
#   1. tag the images running now, so the way back is an image that exists;
#   2. build the new images while the old ones still serve;
#   3. stop comms and lucent;
#   4. the load that counts — unified_db.cutover for broker, sessions and
#      lucent only, which must report zero drift and every check green, or
#      nothing is switched and the old services come straight back;
#   5. raise the write fence on those three stores and their quiet copies;
#   6. start the new services on Postgres and wait for them to answer;
#   7. the postflight proof, including the retired-stores check.
#
# If the new services do not answer, the documented revert runs on its own:
# fence down, previous images retagged, services up on the files. A postflight
# that fails with the services answering is reported, not reverted — a check
# can be wrong as well as a service, and that call is the operator's.

set -uo pipefail

ROOT=/home/daniel/Storage/Dev/hive_mind
cd "$ROOT" || exit 2
STAMP=$(date +%Y%m%d-%H%M%S)
LOG="$ROOT/cutover-$STAMP.log"
RESULT="$LOG.result"
export FENCE_JOURNAL="$ROOT/fence.json"
PY="$ROOT/.venv/bin/python"

# The host reaches hive-postgres on loopback 5433 (DECIDED in 1.2.7); the
# containers reach it by name. Same database, two addresses.
CONTAINER_DSN=$(grep -h '^HIVE_DATABASE_URL=' "$ROOT/.env" | head -1 | cut -d= -f2-)
HOST_DSN=${CONTAINER_DSN/@hive-postgres:5432\//@127.0.0.1:5433/}

exec >>"$LOG" 2>&1
say() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
finish() { echo "$1" >"$RESULT"; say "RESULT: $1"; exit "${2:-0}"; }

wait_healthy() {  # url, seconds
  local url=$1 deadline=$(( $(date +%s) + $2 ))
  while (( $(date +%s) < deadline )); do
    # Any HTTP answer below 500 means the process is up, and both open their
    # pool in the lifespan before serving, so up means the database answered.
    # Comms' /health sits behind its gate and answers 401; `curl -f` would
    # call that down and revert a cutover that worked.
    local code
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 "$url" 2>/dev/null)
    [[ "$code" =~ ^[234][0-9][0-9]$ ]] && return 0
    sleep 2
  done
  return 1
}

back_to_the_files() {
  # Both services bind-mount their source from this checkout, so the old
  # image alone is not the old service: the working tree has to hold the old
  # source as well, and it is LEFT there — restoring HEAD afterwards would
  # put the Postgres code under the old image at its next restart. The tree
  # is dirty on purpose after this, and the log says so.
  docker tag hive_mind-comms:pre-cutover hive_mind-comms:latest
  docker tag hive_mind-lucent:pre-cutover hive_mind-lucent:latest
  # Only the tracked source: docker-compose.yml is per-host and untracked, and
  # one unmatched pathspec makes git restore nothing at all. The compose the
  # old services ran under is the frozen copy beside it.
  if ! git -C "$ROOT" checkout -q "$PRE_SHA" -- nervous-system/comms nervous-system/lucent_api; then
    say "COULD NOT RESTORE THE OLD SOURCE — operator needed"
    return 1
  fi
  docker compose -f "$PRE_COMPOSE" up -d --no-build --force-recreate comms lucent
  wait_healthy http://127.0.0.1:8426/health 120 && wait_healthy http://127.0.0.1:8425/health 120 \
    && say "the services answer on the files again" \
    || say "THE SERVICES DO NOT ANSWER ON THE FILES EITHER — operator needed"
  say "working tree holds $PRE_SHA for nervous-system/comms and lucent_api, and the services run under $PRE_COMPOSE, deliberately"
}

revert() {
  say "REVERTING: the documented sequence from 1.2.18"
  "$PY" -m unified_db.fence --revert || say "fence revert reported something left alone; see above"
  back_to_the_files
}

say "cutover of comms and lucent, log $LOG"
[[ -n "$CONTAINER_DSN" ]] || finish refused 2
PRE_SHA=${PRE_SHA:?set PRE_SHA to the hive_mind commit the running services were built from}
PRE_COMPOSE="$ROOT/docker-compose.pre-cutover.yml"
[[ -f "$PRE_COMPOSE" ]] || { say "no $PRE_COMPOSE, so no way back"; finish refused 2; }
git -C "$ROOT" cat-file -e "$PRE_SHA^{commit}" || { say "$PRE_SHA is not a commit"; finish refused 2; }
say "running services were built from $PRE_SHA; switching to $(git rev-parse --short HEAD)"

say "1. tagging the images the running containers were started from as pre-cutover"
# The image each container is actually running, not whatever `:latest` names:
# a build since the container started moves the tag and leaves the container
# on the old image, and tagging `:latest` would make the way back an image
# that has never served.
docker tag "$(docker inspect -f '{{.Image}}' hive-comms)" hive_mind-comms:pre-cutover || finish refused 2
docker tag "$(docker inspect -f '{{.Image}}' hive-lucent)" hive_mind-lucent:pre-cutover || finish refused 2

git -C /home/daniel/Storage/Dev/hive-data cat-file -e HEAD:hive_data/broker_ops.py 2>/dev/null \
  || { say "the hive-data checkout has no broker_ops; the new comms would not start"; finish refused 2; }

say "2. building the new images while the old ones serve"
docker compose build comms lucent || finish refused 2

say "3. stopping comms and lucent"
docker compose stop comms lucent || finish refused 2

say "4. the load that counts"
if ! "$PY" -m unified_db.cutover --dsn "$HOST_DSN" \
      --only broker --only sessions --only lucent; then
  say "the load is not ready; nothing switched, bringing the old services back"
  back_to_the_files
  finish refused 1
fi

say "5. raising the write fence"
"$PY" -m unified_db.fence --only broker --only sessions --only lucent || { revert; finish reverted 1; }

say "6. starting comms and lucent on Postgres"
# Comms first, lucent only once comms answers: every turn's Stop hook writes a
# memory to lucent, so a lucent that came up on Postgres while comms failed
# would take writes the revert then strands in a database nothing reads.
docker compose up -d --no-build --force-recreate comms
if ! wait_healthy http://127.0.0.1:8426/health 180; then
  docker compose logs --tail 80 comms
  revert
  finish reverted 1
fi
docker compose up -d --no-build --force-recreate lucent
if ! wait_healthy http://127.0.0.1:8425/health 180; then
  docker compose logs --tail 80 lucent
  say "comms was up on Postgres for the lucent wait; anything it wrote in that window stays in Postgres"
  revert
  finish reverted 1
fi
say "both answer"

say "7. postflight"
if HIVE_DATABASE_URL="$HOST_DSN" "$PY" -m unified_db.postflight --dsn "$HOST_DSN"; then
  finish switched 0
fi
finish failed-check 1
