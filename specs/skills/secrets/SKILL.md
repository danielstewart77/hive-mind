---
name: secrets
description: Manage secrets via the system keyring
user-invocable: false
---

# Secrets Tool

Store, check, and list secrets in the system keyring. Values are never revealed in output.

## Usage

### Store a secret
```bash
python /usr/src/app/tools/stateless/secrets/secrets.py set \
  --key "<KEY_NAME>" \
  --value "<secret_value>"
```

### Check if a secret exists
```bash
python /usr/src/app/tools/stateless/secrets/secrets.py get \
  --key "<KEY_NAME>"
```

### List all stored secret keys
```bash
python /usr/src/app/tools/stateless/secrets/secrets.py list
```

## Arguments

### set
- `--key` (required): Secret key name (e.g. "STRIPE_API_KEY"). Must end with _KEY, _SECRET, _TOKEN, _API, _AUTH, _URI, _URL, _EMAIL, _PASSWORD, _ID, or start with HIVEMIND_
- `--value` (required): The secret value to store

### get
- `--key` (required): Secret key name to check

### list
No arguments required.

## Output

JSON with operation results. Set confirms storage. Get confirms existence without revealing value. List returns array of stored key names.
