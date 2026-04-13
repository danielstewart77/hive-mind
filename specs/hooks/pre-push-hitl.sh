#!/bin/bash
# Hive Mind — HITL-gated git push
#
# Sends a Telegram approval request via the gateway's HITL endpoint.
# Blocks until the owner approves or the request times out (180s).
# If denied or timed out, the push is aborted.
#
# Install: copy to .git/hooks/pre-push and chmod +x
#
# Bypass: set SKIP_HITL_PUSH=true to skip the gate (used by nightly
# autonomous runs where no one is awake to approve).
#
# Requires: /hitl/request and /hitl/respond endpoints on the gateway.
# See specs/hitl.md for the full HITL protocol.

GATEWAY_URL="${HIVE_MIND_SERVER_URL:-http://localhost:8420}"

remote="$1"
url="$2"

# Nightly / automated bypass
if [ "${SKIP_HITL_PUSH}" = "true" ]; then
    echo "[HITL] Nightly mode — skipping HITL gate."
    exit 0
fi

# Collect what's being pushed
while read local_ref local_oid remote_ref remote_oid; do
    branch=$(echo "$local_ref" | sed 's|refs/heads/||')
    # Count commits being pushed
    if [ "$remote_oid" = "0000000000000000000000000000000000000000" ]; then
        commits="(new branch)"
    else
        commits="$(git log --oneline ${remote_oid}..${local_oid} 2>/dev/null | wc -l | tr -d ' ') commit(s)"
    fi
    summary="git push to ${branch} — ${commits}"
done

if [ -z "$summary" ]; then
    summary="git push to ${remote}"
fi

echo "[HITL] Requesting push approval: ${summary}"

response=$(curl -s -X POST "${GATEWAY_URL}/hitl/request" \
    -H "Content-Type: application/json" \
    -d "{\"action\": \"git push\", \"summary\": \"${summary}\"}" \
    --max-time 200 2>/dev/null)

approved=$(echo "$response" | grep -o '"approved":\s*true')

if [ -n "$approved" ]; then
    echo "[HITL] Push approved."
    exit 0
else
    echo "[HITL] Push denied or timed out. Aborting."
    exit 1
fi
