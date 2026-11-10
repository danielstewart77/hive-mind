"""Deliverable E (shipped: hive-harness-dsh PR #47, hive-mind PR #352).

As built:
1. The tree, from a clone of hive-harness-dsh, with BuildKit, as the operator:
       docker build --target tree --output type=local,dest="$HIVE_HOME/dsh" .
   (about 1.6 GB; the host web proxy as build args).
2. The profile, from the hive-mind clone's root, stdlib only:
       python3 -m minds.harness.dsh_profile --mind N --out "$HIVE_HOME/minds" [--dsh-root /opt/dsh]
   writes <out>/<N>/.dsh/profiles/hive and prints one JSON line
   {"mind","profile","profile_dir","dsh_home","links","dsh_bin"}; exit 2 {"error"};
   safe to repeat.
3. A container mind: DSH_HOME (writable, in the mind folder), DSH_BIN,
   DSH_READ_LOG_CLI, DSH_PROXY_BASE_URL=<proxy>/v1, DSH_API_KEY=<client key>;
   the tree read-only at /opt/dsh; /tmp exec. Never DSH_MODEL,
   DSH_MODEL_CONTEXT_WINDOW, HIVE_PROXY_KEY or DSH_PERMISSION_MODE.
   runtime.yaml: harness dsh, default_model from the dsh listing,
   runtime_config_dir the DSH_HOME, owner the admin.

4. The hooks, from the hive-mind clone's root, stdlib only:
       python3 -m minds.harness.dsh_hooks --mind N --out "$HIVE_HOME/minds"
   writes <out>/<N>/.dsh/hooks/ (rewritten every run) and <out>/<N>/.dsh/hooks.json
   (only when absent) and prints one JSON line
   {"mind","hooks_dir","hooks_config","hooks_config_written","scripts",
   "hooks_dir_in_container"}; exit 2 {"error"}. The hooks read MIND_ID,
   COMMS_URL/COMMS_BEARER_TOKEN, LUCENT_URL_SELF/LUCENT_BEARER_TOKEN and, when
   tools is installed, HIVE_TOOLS_URL/HIVE_TOOLS_TOKEN; without tools a turn
   is not captured to memory, and dsh is still offered. A clone predating the
   generator fails the hooks step, naming why.
"""

from __future__ import annotations

from pathlib import Path

MOUNT = "/opt/dsh"
BIN = f"{MOUNT}/apps/cli/lib/bin.js"
READ_LOG_CLI = f"{MOUNT}/packages/session/session-persistence-jsonl/lib/read-log-cli.js"
PROFILE = "hive"
LAUNCHER = Path("apps/cli/lib/bin.js")
PROFILE_MODULE = "minds.harness.dsh_profile"
#: The hooks generator the follow-up is delivering (assumed name and flags:
#: the same shape as the profile generator).
HOOKS_MODULE = "minds.harness.dsh_hooks"
#: Where the mind's container sees the minds folder.
CONTAINER_MINDS = "/usr/src/app/minds"


def build_tree(repo: Path, dest: Path, proxy_args: dict[str, str]) -> list[str]:
    argv = ["docker", "build", "--target", "tree",
            "--output", f"type=local,dest={dest}"]
    for key, value in proxy_args.items():
        argv += ["--build-arg", f"{key}={value}"]
    return [*argv, str(repo)]


def tree_built(dest: Path) -> bool:
    return (dest / LAUNCHER).is_file()


def profile_command(mind: str, minds_dir: Path) -> list[str]:
    """Run with cwd = the hive-mind clone."""
    return ["python3", "-m", PROFILE_MODULE, "--mind", mind, "--out", str(minds_dir),
            "--dsh-root", MOUNT]


def hooks_command(mind: str, minds_dir: Path) -> list[str]:
    """Run with cwd = the hive-mind clone."""
    return ["python3", "-m", HOOKS_MODULE, "--mind", mind, "--out", str(minds_dir),
            "--container-minds-dir", CONTAINER_MINDS]


def profile_path(mind_dir: Path) -> Path:
    return mind_dir / ".dsh" / "profiles" / PROFILE


def mind_environment(*, app_mind_dir: str, proxy_url: str, key_ref: str) -> dict[str, str]:
    root = proxy_url.rstrip("/")
    root = root[: -len("/v1")] if root.endswith("/v1") else root
    return {
        "DSH_BIN": BIN,
        "DSH_HOME": f"{app_mind_dir}/.dsh",
        "DSH_READ_LOG_CLI": READ_LOG_CLI,
        "DSH_PROXY_BASE_URL": f"{root}/v1",
        "DSH_API_KEY": key_ref,
        # What the mind's own model listing (minds/models_api.py) reads, so
        # the console's model picker is offered the dsh listing too.
        "INFERENCE_PROXY_URL": root,
        "MIND_PROXY_KEY": key_ref,
    }


def runtime_keys(app_mind_dir: str) -> dict[str, str]:
    """What runtime.yaml says for a dsh mind, beyond its name and model."""
    return {
        "harness": "dsh",
        "provider": "proxy",
        "runtime_config_dir": f"{app_mind_dir}/.dsh",
        "dsh_profile": PROFILE,
        "dsh_bin": BIN,
    }
