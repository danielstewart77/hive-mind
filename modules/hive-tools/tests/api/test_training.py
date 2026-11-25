"""API tests for the /training router.

The router is a transport over hive_mind's stateless pipeline CLI, so these
tests pin the transport contract: arguments are translated faithfully, the
CLI's JSON is passed through unchanged, a missing mount is a clear 503
rather than a stack trace, and launching a fine-tune is never the default.
"""

from __future__ import annotations

from collections.abc import Generator

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import store
from tests.scratch import db, run


@pytest.fixture()
def app(tmp_path: Path) -> FastAPI:
    from server import create_app

    return create_app()


@pytest.fixture()
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    # Entered, not merely constructed: the store opens in the app's
    # lifespan, and a client that never enters it is an app whose database
    # was never opened.
    with TestClient(app) as entered:
        yield entered


@pytest.fixture()
def auth_headers(client: TestClient) -> dict[str, str]:
    from auth_api import generate_token

    raw = run(generate_token("ada"))
    return {"Authorization": f"Bearer {raw}"}


@pytest.fixture(autouse=True)
def hitl_off(client: TestClient):
    """Pin the gate open regardless of what the seed defaults happen to be.

    Production seeds these off, since the console's login is already the
    human in the loop. Asserting it here would only restate the seed test;
    what matters is that a changed default never silently turns these tests
    into assertions about a 202 approval envelope.
    """
    for tool in (
        "training_curate",
        "training_export",
        "training_train",
        "training_deploy",
    ):
        run(store.set_setting(tool, "off", 15))


@pytest.fixture()
def fake_cli(tmp_path: Path, monkeypatch):
    """Point the router at a CLI path that exists, and capture invocations."""
    import tools.training as training

    hive_dir = tmp_path / "hive_mind"
    cli = hive_dir / training.CLI_RELATIVE
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text("# stub\n")
    monkeypatch.setattr(training, "HIVE_MIND_DIR", hive_dir)

    calls: list[list[str]] = []

    def _completed(payload: dict, returncode: int = 0):
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=json.dumps(payload), stderr=""
        )

    return {"calls": calls, "completed": _completed, "dir": hive_dir}


def _patch_run(fake_cli, payload, returncode=0):
    def _fake_run(command, **kwargs):
        fake_cli["calls"].append(command)
        return fake_cli["completed"](payload, returncode)

    return patch("tools.training.subprocess.run", side_effect=_fake_run)


def _args(fake_cli) -> list[str]:
    """CLI arguments of the last invocation, without interpreter and path."""
    return fake_cli["calls"][-1][2:]


def test_status_passes_the_cli_payload_through(client, auth_headers, fake_cli):
    payload = {"total": 8809, "by_flag": {"keep": 3024}, "exists": True}
    with _patch_run(fake_cli, payload):
        response = client.get("/training/status", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == payload
    assert _args(fake_cli) == ["status"]


def test_curate_translates_every_knob(client, auth_headers, fake_cli):
    with _patch_run(fake_cli, {"kept": 3024}):
        response = client.post(
            "/training/curate",
            headers=auth_headers,
            json={
                "keep_per_cluster": 5,
                "min_length_tokens": 100,
                "exclude_secret_rows": True,
                "harness": ["codex"],
            },
        )
    assert response.status_code == 200
    args = _args(fake_cli)
    assert args[0] == "curate"
    assert "--keep-per-cluster" in args and args[args.index("--keep-per-cluster") + 1] == "5"
    assert args[args.index("--min-length-tokens") + 1] == "100"
    assert "--exclude-secret-rows" in args
    assert args[args.index("--harness") + 1] == "codex"


def test_curate_reset_sends_only_the_reset_flag(client, auth_headers, fake_cli):
    with _patch_run(fake_cli, {"reset_rows": 8809}):
        response = client.post(
            "/training/curate", headers=auth_headers, json={"reset": True}
        )
    assert response.status_code == 200
    assert _args(fake_cli) == ["curate", "--reset"]


def test_curate_rejects_a_nonsense_cluster_budget(client, auth_headers, fake_cli):
    response = client.post(
        "/training/curate", headers=auth_headers, json={"keep_per_cluster": 0}
    )
    assert response.status_code == 422


def test_export_translates_mode_and_filters(client, auth_headers, fake_cli):
    with _patch_run(fake_cli, {"train_examples": 2840}):
        response = client.post(
            "/training/export",
            headers=auth_headers,
            json={
                "mode": "reasoning",
                "name": "v1",
                "eval_fraction": 0.1,
                "require_reasoning": True,
                "include_system_prompt": False,
            },
        )
    assert response.status_code == 200
    args = _args(fake_cli)
    assert args[args.index("--mode") + 1] == "reasoning"
    assert args[args.index("--name") + 1] == "v1"
    assert "--require-reasoning" in args
    assert "--no-system-prompt" in args


def test_export_defaults_to_randomizing_credentials(client, auth_headers, fake_cli):
    """Same default as the CLI: a surrogate, never a real value by accident."""
    with _patch_run(fake_cli, {"train_examples": 1}):
        client.post("/training/export", headers=auth_headers, json={"name": "raw"})
    args = _args(fake_cli)
    assert args[args.index("--secrets") + 1] == "randomize"


def test_export_forwards_the_chosen_secret_policy(client, auth_headers, fake_cli):
    for policy in ("keep", "redact"):
        with _patch_run(fake_cli, {"train_examples": 1}):
            client.post(
                "/training/export",
                headers=auth_headers,
                json={"name": "shareable", "secrets": policy},
            )
        args = _args(fake_cli)
        assert args[args.index("--secrets") + 1] == policy


def test_export_rejects_an_unknown_secret_policy(client, auth_headers, fake_cli):
    response = client.post(
        "/training/export", headers=auth_headers, json={"secrets": "yolo"}
    )
    assert response.status_code == 422


def test_export_rejects_an_unknown_mode(client, auth_headers, fake_cli):
    response = client.post(
        "/training/export", headers=auth_headers, json={"mode": "vibes"}
    )
    assert response.status_code == 422


def test_train_defaults_to_a_dry_run(client, auth_headers, fake_cli):
    """A launch takes the whole GPU; it must never be the fallthrough."""
    with _patch_run(fake_cli, {"can_run": False, "blockers": ["gpu busy"]}):
        response = client.post(
            "/training/train", headers=auth_headers, json={"train_file": "/x/train.jsonl"}
        )
    assert response.status_code == 200
    assert "--dry-run" in _args(fake_cli)


def test_train_omits_dry_run_only_when_explicitly_disabled(
    client, auth_headers, fake_cli
):
    with _patch_run(fake_cli, {"launched": True}):
        client.post(
            "/training/train",
            headers=auth_headers,
            json={"train_file": "/x/train.jsonl", "dry_run": False},
        )
    assert "--dry-run" not in _args(fake_cli)


def test_runs_clamps_the_limit_and_validates_kind(client, auth_headers, fake_cli):
    with _patch_run(fake_cli, {"runs": []}):
        client.get("/training/runs?limit=9999", headers=auth_headers)
    assert _args(fake_cli)[_args(fake_cli).index("--limit") + 1] == "200"

    response = client.get("/training/runs?kind=nonsense", headers=auth_headers)
    assert response.status_code == 400


def test_missing_repo_mount_is_a_clear_503(client, auth_headers, tmp_path, monkeypatch):
    import tools.training as training

    monkeypatch.setattr(training, "HIVE_MIND_DIR", tmp_path / "absent")
    response = client.get("/training/status", headers=auth_headers)
    assert response.status_code == 503
    assert "mounted" in response.json()["detail"]


def test_cli_failure_surfaces_its_json_error(client, auth_headers, fake_cli):
    with _patch_run(fake_cli, {"error": "database is locked"}, returncode=1):
        response = client.get("/training/status", headers=auth_headers)
    assert response.status_code == 500
    assert response.json()["detail"]["error"] == "database is locked"


def test_non_json_output_is_reported_not_swallowed(client, auth_headers, fake_cli):
    def _fake_run(command, **kwargs):
        fake_cli["calls"].append(command)
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="ModuleNotFoundError: core", stderr=""
        )

    with patch("tools.training.subprocess.run", side_effect=_fake_run):
        response = client.get("/training/status", headers=auth_headers)
    assert response.status_code == 500
    assert "non-JSON" in response.json()["detail"]


def test_timeout_is_reported_as_gateway_timeout(client, auth_headers, fake_cli):
    def _fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1800)

    with patch("tools.training.subprocess.run", side_effect=_fake_run):
        response = client.get("/training/status", headers=auth_headers)
    assert response.status_code == 504


def test_mutating_endpoints_are_hitl_gated(client, auth_headers, app, fake_cli):
    """With the gate on, a curate request waits for approval instead of running."""
    db.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode) VALUES (?, ?) "
        "ON CONFLICT (tool_name) DO UPDATE SET mode = excluded.mode, "
        "sudo_timeout_minutes = excluded.sudo_timeout_minutes",
        ("training_curate", "on"),
    )
    db.commit()
    with _patch_run(fake_cli, {"kept": 1}):
        response = client.post("/training/curate", headers=auth_headers, json={})
    assert response.status_code == 202
    assert fake_cli["calls"] == []


def test_every_endpoint_requires_a_token(client):
    assert client.get("/training/status").status_code in (401, 403)
    assert client.post("/training/curate", json={}).status_code in (401, 403)
    assert client.post("/training/export", json={}).status_code in (401, 403)
    assert (
        client.post("/training/train", json={"train_file": "/x"}).status_code
        in (401, 403)
    )


def test_base_models_reaches_the_catalog_subcommand(client, auth_headers, fake_cli):
    payload = {"default": "Qwen/Qwen3-8B", "models": [{"id": "Qwen/Qwen3-8B"}]}
    with _patch_run(fake_cli, payload):
        response = client.get("/training/base-models", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == payload
    args = _args(fake_cli)
    assert args[0] == "base-models"
    assert "--ollama-url" in args


def test_base_models_requires_a_token(client, fake_cli):
    assert client.get("/training/base-models").status_code == 401


def test_train_translates_the_full_knob_set(client, auth_headers, fake_cli):
    with _patch_run(fake_cli, {"can_run": True}):
        response = client.post(
            "/training/train",
            headers=auth_headers,
            json={
                "train_file": "/data/v1/train.jsonl",
                "base_model": "Qwen/Qwen3-14B",
                "lora_rank": 16,
                "lora_alpha": 128,
                "lora_dropout": 0.1,
                "epochs": 3,
                "batch_size": 2,
                "gradient_accumulation_steps": 8,
                "max_sequence_length": 4096,
                "warmup_ratio": 0.1,
                "load_in_4bit": False,
                "seed": 5,
            },
        )
    assert response.status_code == 200
    args = _args(fake_cli)
    assert args[args.index("--base-model") + 1] == "Qwen/Qwen3-14B"
    assert args[args.index("--lora-rank") + 1] == "16"
    assert args[args.index("--lora-alpha") + 1] == "128"
    assert args[args.index("--lora-dropout") + 1] == "0.1"
    assert args[args.index("--epochs") + 1] == "3"
    assert args[args.index("--batch-size") + 1] == "2"
    assert args[args.index("--gradient-accumulation-steps") + 1] == "8"
    assert args[args.index("--max-sequence-length") + 1] == "4096"
    assert args[args.index("--warmup-ratio") + 1] == "0.1"
    assert args[args.index("--seed") + 1] == "5"
    assert "--no-4bit" in args


def test_four_bit_loading_is_the_default_and_passes_no_flag(
    client, auth_headers, fake_cli
):
    with _patch_run(fake_cli, {"can_run": True}):
        client.post(
            "/training/train",
            headers=auth_headers,
            json={"train_file": "/data/v1/train.jsonl"},
        )
    assert "--no-4bit" not in _args(fake_cli)


def test_an_unset_lora_alpha_is_left_to_the_cli(client, auth_headers, fake_cli):
    """The CLI derives it from the rank; sending 0 would override with junk."""
    with _patch_run(fake_cli, {"can_run": True}):
        client.post(
            "/training/train",
            headers=auth_headers,
            json={"train_file": "/data/v1/train.jsonl"},
        )
    assert "--lora-alpha" not in _args(fake_cli)


def test_deploy_translates_the_request(client, auth_headers, fake_cli):
    with _patch_run(fake_cli, {"deployed": True, "stage": "served"}):
        response = client.post(
            "/training/deploy",
            headers=auth_headers,
            json={
                "name": "skippy-lora",
                "adapter_dir": "/data/v1/adapter",
                "base_model": "Qwen/Qwen3-8B",
                "quantization": "q5_K_M",
                "num_ctx": 16384,
            },
        )
    assert response.status_code == 200
    args = _args(fake_cli)
    assert args[0] == "deploy"
    assert args[args.index("--name") + 1] == "skippy-lora"
    assert args[args.index("--adapter-dir") + 1] == "/data/v1/adapter"
    assert args[args.index("--quantization") + 1] == "q5_K_M"
    assert args[args.index("--num-ctx") + 1] == "16384"


def test_deploy_rejects_a_model_name_ollama_would_choke_on(
    client, auth_headers, fake_cli
):
    response = client.post(
        "/training/deploy",
        headers=auth_headers,
        json={"name": "skippy lora; rm -rf /", "adapter_dir": "/data/v1/adapter"},
    )
    assert response.status_code == 422


def test_deploy_passes_a_refusal_through_rather_than_erroring(
    client, auth_headers, fake_cli
):
    """A refused deploy is an outcome the console renders, not a 500."""
    payload = {"deployed": False, "stage": "refused", "blockers": ["no adapter weights"]}
    with _patch_run(fake_cli, payload):
        response = client.post(
            "/training/deploy",
            headers=auth_headers,
            json={"name": "skippy-lora", "adapter_dir": "/data/v1/adapter"},
        )
    assert response.status_code == 200
    assert response.json() == payload


# ----------------------------------------------- GPU arbitration at the route


@pytest.fixture()
def arbitration(tmp_path, monkeypatch):
    """Replace the card, the daemon and Telegram with observable stand-ins."""
    from tools import training_gpu

    journal = tmp_path / "gpu_arbitration.json"
    monkeypatch.setattr(training_gpu, "JOURNAL_PATH", journal)
    state = {
        "preflighted": [],
        "restored": [],
        "watched": [],
        "notified": [],
        "in_flight": None,
    }

    async def fake_preflight(output_name, run_id="", containers=None, path=None):
        state["preflighted"].append(output_name)
        entry = {"output_name": output_name, "restore": ["hive-mind-voice"]}
        training_gpu.write_journal(entry, journal)
        return entry

    async def fake_restore(entry, path=None):
        state["restored"].append(entry["output_name"])
        training_gpu.clear_journal(journal)
        return {"started": entry.get("restore", []), "failures": []}

    async def fake_watch(entry, notify=None, poll_seconds=None, path=None, close_run=None):
        state["watched"].append(entry["output_name"])
        return {"outcome": "succeeded"}

    async def fake_notify(message):
        state["notified"].append(message)
        return True

    monkeypatch.setattr(training_gpu, "preflight", fake_preflight)
    monkeypatch.setattr(training_gpu, "restore", fake_restore)
    monkeypatch.setattr(training_gpu, "watch", fake_watch)
    monkeypatch.setattr(training_gpu, "notify_owner", fake_notify)
    monkeypatch.setattr(training_gpu, "run_in_flight", lambda path=None: state["in_flight"])
    return state


def _train_body(**kwargs):
    body = {"train_file": "/data/v1/train.jsonl", "name": "run-a", "dry_run": False}
    body.update(kwargs)
    return body


def test_launching_frees_the_card_and_arms_the_watcher(
    client, auth_headers, fake_cli, arbitration
):
    """Requirement 1 and 9 at the seam that actually runs in production."""
    with _patch_run(fake_cli, {"launched": True, "run_id": "r1"}):
        response = client.post("/training/train", json=_train_body(), headers=auth_headers)

    assert response.status_code == 200
    assert arbitration["preflighted"] == ["run-a"]
    assert arbitration["watched"] == ["run-a"]
    assert arbitration["restored"] == []
    assert any("voice is down" in m for m in arbitration["notified"])


def test_a_refused_launch_gives_the_card_straight_back(
    client, auth_headers, fake_cli, arbitration
):
    """Requirement 2: nothing is training, so nothing should stay down."""
    with _patch_run(fake_cli, {"launched": False, "plan": {"blockers": ["GPU busy"]}}):
        response = client.post("/training/train", json=_train_body(), headers=auth_headers)

    assert response.status_code == 200
    assert arbitration["restored"] == ["run-a"]
    assert arbitration["watched"] == []


def test_a_launch_that_errors_gives_the_card_straight_back(
    client, auth_headers, fake_cli, arbitration
):
    """The path that used to leave the machine mute: docker or the CLI dying."""
    with _patch_run(fake_cli, {"detail": "boom"}, returncode=1):
        response = client.post("/training/train", json=_train_body(), headers=auth_headers)

    assert response.status_code == 500
    assert arbitration["restored"] == ["run-a"]


def test_a_dry_run_never_touches_the_card(client, auth_headers, fake_cli, arbitration):
    """Requirement 7's sibling: planning is not launching."""
    with _patch_run(fake_cli, {"can_run": True, "blockers": []}):
        response = client.post(
            "/training/train", json=_train_body(dry_run=True), headers=auth_headers
        )

    assert response.status_code == 200
    assert arbitration["preflighted"] == []


def test_curate_and_export_never_touch_the_card(
    client, auth_headers, fake_cli, arbitration
):
    """Requirement 7, driven through the routes rather than the functions.

    Called at this level because the regression it guards against is
    someone hoisting preflight into the shared CLI wrapper, which a test
    calling the curate handler directly would not see.
    """
    with _patch_run(fake_cli, {"kept": 10}):
        client.post("/training/curate", json={}, headers=auth_headers)
    with _patch_run(fake_cli, {"written": 10}):
        client.post("/training/export", json={}, headers=auth_headers)

    assert arbitration["preflighted"] == []
    assert arbitration["restored"] == []


def test_a_second_launch_is_refused_while_one_is_in_flight(
    client, auth_headers, fake_cli, arbitration
):
    """Requirement 11: one card, one trainer."""
    arbitration["in_flight"] = {"output_name": "run-a", "started_at": 1}

    with _patch_run(fake_cli, {"launched": True, "run_id": "r2"}):
        response = client.post(
            "/training/train", json=_train_body(name="run-b"), headers=auth_headers
        )

    assert response.status_code == 409
    assert arbitration["preflighted"] == []


def test_deploying_during_a_run_is_refused(client, auth_headers, fake_cli, arbitration):
    """A merge launches a GPU container; an adapter upload wakes Ollama."""
    arbitration["in_flight"] = {"output_name": "run-a", "started_at": 1}

    with _patch_run(fake_cli, {"deployed": True}):
        response = client.post(
            "/training/deploy",
            json={"name": "skippy-lora", "adapter_dir": "/data/v1/adapter"},
            headers=auth_headers,
        )

    assert response.status_code == 409


def test_both_triggers_take_the_same_guarded_path(
    client, auth_headers, fake_cli, arbitration
):
    """Requirement 8, asserted as behaviour rather than symbol identity.

    An agent posting to this route and the console button pressing it are
    the same request; what matters is that neither can reach the launcher
    without acquiring and releasing the card, so the in-flight guard is
    asserted to bite regardless of who is calling.
    """
    arbitration["in_flight"] = {"output_name": "run-a", "started_at": 1}
    with _patch_run(fake_cli, {"launched": True}):
        agent_call = client.post(
            "/training/train", json=_train_body(name="agent-run"), headers=auth_headers
        )
        console_call = client.post(
            "/training/train", json=_train_body(name="console-run"), headers=auth_headers
        )
    assert agent_call.status_code == console_call.status_code == 409
