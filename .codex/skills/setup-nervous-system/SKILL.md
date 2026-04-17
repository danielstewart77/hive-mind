---
name: setup-nervous-system
description: Bootstrap and verify the Hive Mind nervous system — gateway server, broker, session manager, SQLite, Neo4j, and MCP servers. Deploys from scratch if nothing exists.
---

# setup-nervous-system

Deploy and verify the core infrastructure. Everything else depends on this.

## Step 1 — Docker prerequisites

```bash
docker info > /dev/null 2>&1 || echo "BLOCKER: Docker not running"
docker network ls --filter name=hive_mind_net --format "{{.Name}}" | grep -q hive_mind_net || docker network create hive_mind_net
```

## Step 2 — Build and deploy gateway

```bash
docker compose --profile ${COMPOSE_PROFILES:-cpu-only} up -d --build server
```

Wait for health:
```bash
for i in $(seq 1 30); do curl -sf http://localhost:8420/sessions > /dev/null && break; sleep 2; done
```

If not healthy after 60s, check logs: `docker logs hive-mind-server --tail 30`

## Step 3 — Deploy Neo4j

```bash
docker compose up -d neo4j
```

Wait for ready:
```bash
for i in $(seq 1 30); do curl -sf http://localhost:7474 > /dev/null && break; sleep 2; done
```

Check/store secrets:
- NEO4J_AUTH: `python3 -m keyring get hive-mind NEO4J_AUTH 2>/dev/null`
- NEO4J_URI: `python3 -m keyring get hive-mind NEO4J_URI 2>/dev/null`
- If missing, ask user and store via secrets tool

## Step 4 — Verify broker and sessions

```bash
curl -sf http://localhost:8420/broker/minds | jq length
curl -sf http://localhost:8420/sessions | jq length
docker exec hive-mind-server ls -la /usr/src/app/data/
```

Verify sessions.db and broker.db exist.

## Step 5 — Verify MCP

Read `.mcp.json` and `.mcp.container.json`. Verify configured paths exist.

## Step 6 — Report

Present status table:

| Component | Status | Details |
|-----------|--------|---------|
| Docker | OK/FAIL | version |
| Network | OK/FAIL | hive_mind_net |
| Gateway (8420) | OK/FAIL | container status |
| Broker | OK/FAIL | N minds registered |
| Session Manager | OK/FAIL | N active sessions |
| SQLite DBs | OK/FAIL | sessions.db, broker.db |
| Neo4j (7474) | OK/FAIL | container status |
| MCP config | OK/FAIL | paths verified |
