---
name: generate-compose
description: Read all MIND.md files with container blocks and generate docker-compose.yml service definitions between the GENERATED MINDS markers. Use after adding or modifying a containerised mind.
---

# generate-compose

Scan all `minds/*/MIND.md` files for `container:` blocks and regenerate the per-mind service definitions in `docker-compose.yml`.

## Step 1 — Scan MIND.md files

For each subdirectory in `minds/`:
```bash
for d in minds/*/; do
  name=$(basename "$d")
  [[ "$name" == "__pycache__" ]] && continue
  [ -f "$d/MIND.md" ] && echo "$name"
done
```

For each mind with a MIND.md, parse the frontmatter. Look for a `container:` block. If present, this mind gets its own service definition.

## Step 2 — Generate service blocks

For each mind with a `container:` block, generate a docker-compose service definition:

```yaml
  <name>:
    image: <container.image or "hive_mind:latest">
    container_name: hive-mind-<name>
    environment:
      - MIND_ID=<name>
      - HOME=/home/<name>
      - PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring
      - XDG_DATA_HOME=/home/<name>/.claude/data
      <any additional container.environment entries>
    volumes:
      - ${HOST_PROJECT_DIR:-.}:/usr/src/app:ro
      - <name>_home:/home/<name>
      <any container.volumes entries>
    restart: unless-stopped
    networks:
      - hive_mind_net
    depends_on:
      - server
```

## Step 3 — Generate volume entries

For each containerised mind, add a named volume:

```yaml
  <name>_home:
```

## Step 4 — Write to docker-compose.yml

Replace everything between the markers:
- `# BEGIN GENERATED MINDS` / `# END GENERATED MINDS` for service definitions
- `# BEGIN GENERATED VOLUMES` / `# END GENERATED VOLUMES` for volume definitions

Read the current `docker-compose.yml`, replace the content between markers, write back.

**Important:** Do NOT modify anything outside the markers. The nervous system, body, and infrastructure services are hand-maintained.

## Step 5 — Validate

```bash
docker compose config > /dev/null 2>&1 && echo "Compose config valid" || echo "INVALID — check syntax"
```

## Step 6 — Report

List which minds were generated, which were skipped (no container block), and validation result.
