# Remote Hive Mind Install — Order of Operations

A guide for installing Hive Mind on a remote machine using the `/hivemind:remote-admin` and `/hivemind:setup-remote` skills.

---

## Why order matters

The `/hivemind:setup-remote` skill uses Claude Code skills to do its work. Those skills can't run unless Claude Code is already installed on the remote machine. **Claude must come before the plugin, which must come before setup.**

Getting this backwards is easy — if your goal is a Hive Mind deployment, it's natural to go straight to `/setup-remote`. But setup requires skills, skills require the plugin, and the plugin requires Claude. Skipping to setup first just fails silently.

---

## Correct order

```
1. Target machine has OS installed + SSH accessible
2. Remote-admin bridge has credentials for the host
3. Install Claude Code on remote (interactive auth required — see below)
4. Install hivemind-claude-plugin on remote
5. Run /hivemind:setup (or /hivemind:setup-remote from the operator machine)
```

---

## Step-by-step

### 1. Prepare SSH access

Use `/hivemind:setup-remote` to enroll an SSH key if you haven't already. On first contact with a new machine you'll use a bootstrap password (one-time), after which key auth takes over.

**Sudo strategy — avoid password in transcript:**

If the machine requires a sudo password, you have two options:

**Option A (recommended for clean setups): Temporary NOPASSWD**

On the remote machine, before starting:
```bash
echo "$USER ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/hive-temp
```

This lets setup proceed without any password touching the conversation transcript. After setup completes:
```bash
sudo rm /etc/sudoers.d/hive-temp
```

Then configure your real sudo strategy (`stored_password` in keyring, or leave passwordless if it's a low-risk machine).

**Option B: Stored password via keyring**

The `setup-remote` skill can store the sudo password in the keyring using `remote_admin_sudo_<host_underscored>`. It's never logged — it goes directly to keyring storage. But the user must type it into the conversation, which stays in the session transcript. Use Option A when possible.

---

### 2. Install Claude Code on the remote machine

Claude Code requires interactive browser auth on first install. This cannot be automated headlessly in the same way as the rest of setup.

**Via remote-admin WebSocket shell:**
```bash
websocat "ws://localhost:8430/sessions/<SID>/stream?token=$TOKEN"
# Then inside the remote shell:
curl -fsSL https://claude.ai/install.sh | sh
claude auth login
# Follow the browser URL — will print a link to open locally
```

The auth flow prints a URL you open in a browser on any machine. Paste the code back into the terminal. This works headlessly — the browser step happens on your local machine, not the remote.

---

### 3. Install the plugin on remote

```bash
# Via remote shell:
claude /plugin marketplace add danielstewart77/hivemind-plugin
```

---

### 4. Run setup

```bash
/hivemind:setup all
```

Or for a minimal install (no Neo4j, no Telegram bot, just Claude + broker):
```bash
/hivemind:setup --minimal
```

---

## What the remote-admin bridge provides

The `/hivemind:remote-admin` skill connects to `http://hive-mind-remote-admin:8430`. Operations available:

- `connect` — open SSH session (key auth from keyring, keyed by `TELEGRAM_USER_ID`)
- `exec` — run a command, get stdout/stderr/exit_code
- `shell` — interactive WebSocket shell (needed for auth flows)
- `close` — close session

For privileged commands, `sudorun()` injects the stored sudo password automatically:
```bash
SUDO_KEY="remote_admin_sudo_$(echo $TARGET_HOST | tr '.' '_')"
# password stored at this keyring key, never echoed
```

---

## Known issues / lessons learned

- **Running setup before Claude is installed:** Setup invokes skills; skills require Claude Code. Always install Claude first.
- **Ed25519 keys:** The remote-admin bridge supports all key types (Ed25519, RSA, ECDSA). If you get a key-type error, check you're running the latest `services/remote_admin.py`.
- **Container hostname:** From inside the Ada container, the bridge is at `http://hive-mind-remote-admin:8430`, not `localhost:8430`.
- **Sudo password with voice transcription:** If using voice input, passwords may get extra spaces added. Confirm the exact string before storing.
