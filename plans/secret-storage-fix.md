# Secret Storage — Fix XDG_DATA_HOME

> **Status:** Identified. Not yet fixed.
> **Urgency:** High — secrets currently evaporate or become inaccessible when Claude config changes.

---

## The Problem

All containers set `XDG_DATA_HOME=/home/hivemind/.claude/data`, which causes
Python's `PlaintextKeyring` to store secrets at:

```
/home/hivemind/.claude/data/python_keyring/keyring_pass.cfg
```

That path lives inside the Claude bind mount (`HOST_CLAUDE_DIR`). This means:

- Secrets are entangled with Claude config — conceptually wrong
- The mount is `:ro` in the mind containers (ada, bob, bilby, nagatha), so those
  containers cannot write new secrets
- Any change to the Claude config setup risks losing or orphaning the keyring file
- New tokens stored by one container may not be visible to others if their
  `XDG_DATA_HOME` resolves differently

---

## The Fix

Point `XDG_DATA_HOME` at the project `data/` directory, which already has a
host bind mount and holds `sessions.db`:

```yaml
# docker-compose.yml — all containers
environment:
  - XDG_DATA_HOME=/usr/src/app/data
```

The keyring would then live at:
```
/usr/src/app/data/python_keyring/keyring_pass.cfg
  ↕ (bind mount)
${HOST_PROJECT_DIR}/data/python_keyring/keyring_pass.cfg
```

This is already on the host, already bind-mounted read-write, already excluded
from Docker named volumes, and has nothing to do with Claude.

---

## Migration

1. Update `XDG_DATA_HOME` in docker-compose.yml for all containers
2. Copy existing `keyring_pass.cfg` from old location to new location on the host
3. Rebuild containers
4. Verify secrets are accessible via `/secrets` skill

---

## Also Note

- `planka-db` and `planka-data` are named volumes — Planka data will be lost
  on volume prune. Low priority since Planka replacement is planned.
- `whisper-cache` is a named volume — Whisper model weights re-download on
  volume prune. Low priority.
