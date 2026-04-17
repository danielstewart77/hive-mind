---
name: secrets
description: Manage secrets via the system keyring
---

# Secrets Tool

Store, retrieve, delete, and list secrets in the system keyring. Uses the
keyring CLI directly for all operations.

## Usage

### Retrieve a secret

```bash
python3 -m keyring get hive-mind "<KEY_NAME>"
```

With environment variable fallback (useful in container contexts):
```bash
VALUE=$(python3 -m keyring get hive-mind "<KEY_NAME>" 2>/dev/null || echo "$<KEY_NAME>")
```

### Store a secret

```bash
echo "<secret_value>" | python3 -m keyring set hive-mind "<KEY_NAME>"
```

### Delete a secret

```bash
python3 -m keyring del hive-mind "<KEY_NAME>"
```

### List all stored secret keys

```bash
python3 -c "import keyring; keys = keyring.get_password('hive-mind', '_KEY_REGISTRY'); print(keys or '[]')"
```

## Key naming guidance

Key names should end with one of: _KEY, _SECRET, _TOKEN, _API, _AUTH, _URI,
_URL, _EMAIL, _PASSWORD, _ID, or start with HIVEMIND_. This is not enforced
by the keyring CLI but is the project convention.

## Output

- `get` prints the secret value to stdout (empty output if not found)
- `set` reads the value from stdin and stores it silently
- `del` removes the key silently
- `list` prints a JSON array of stored key names
