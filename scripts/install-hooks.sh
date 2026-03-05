#!/bin/bash
# Install Hive Mind git hooks.
# Safe to run multiple times -- backs up existing hooks.

HOOKS_DIR="$(git rev-parse --show-toplevel)/.git/hooks"
SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"

# Install pre-commit hook
TARGET="$HOOKS_DIR/pre-commit"
if [ -f "$TARGET" ] && [ ! -L "$TARGET" ]; then
    echo "Backing up existing pre-commit hook to pre-commit.bak"
    cp "$TARGET" "$TARGET.bak"
fi
cp "$SCRIPTS_DIR/pre-commit-pip-audit.sh" "$TARGET"
chmod +x "$TARGET"
echo "Installed pre-commit hook (pip-audit)"

# Install pre-push hook
TARGET="$HOOKS_DIR/pre-push"
if [ -f "$TARGET" ] && [ ! -L "$TARGET" ]; then
    echo "Backing up existing pre-push hook to pre-push.bak"
    cp "$TARGET" "$TARGET.bak"
fi
cp "$SCRIPTS_DIR/pre-push.sh" "$TARGET"
chmod +x "$TARGET"
echo "Installed pre-push hook (HITL gate)"

echo "Done. Hooks installed."
