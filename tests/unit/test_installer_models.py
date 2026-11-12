"""The hive's embedding and classifier models: registered on a proxy the
installer runs, and checked on any proxy before lucent is pointed at them.

The one stub is `subprocess.run`, where the process stops being ours: every
`docker compose exec ... app.cli` and every request made from inside the hive
network (a `docker run` of the HTTP probe) goes through it. It answers the
way the proxy does.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from installer import compose, steps
from installer.runner import NeedInput
from installer.shell import Shell, StepFailed

from tests.unit.installer_helpers import answers, installer_at

EXISTING = "http://proxy.corp.example:8888"
OPERATOR_KEY = "the-operator-key"  # invented


class _Hive:
    """`subprocess.run` for an install: the proxy's command and its routes."""

    def __init__(self, *, chat=("claude-sonnet-5", "gpt-oss:20b-32k"),
                 embedders=("qwen3-embedding:8b",), dimensions=4096, purposes=True,
                 call_status=200):
        self.chat, self.embedders, self.dimensions = list(chat), list(embedders), dimensions
        # What a test embedding or classification answers.
        self.call_status = call_status
        # An older proxy marks no model with a purpose and ignores the filter.
        self.purposes = purposes
        self.commands: list[list[str]] = []
        self.requests: list[dict] = []

    def _cli(self, args: list[str]) -> dict:
        command = args[0]
        if command == "mint-key":
            return {"client": "wren", "created": True, "minted": True, "key": "minted-key"}
        if command == "check-model":
            model = args[args.index("--model") + 1]
            if model in self.embedders:
                return {"model": model, "ok": True, "status": 200, "dimensions": self.dimensions}
            return {"ok": True, "status": 200, "reply": "ok"}
        return {"created": True, "updated": False}

    def _http(self, payload: dict) -> dict:
        self.requests.append(payload)
        url = payload["url"]
        if url.endswith("/health"):
            return {"status": 200, "body": "{}"}
        if "/v1/models" in url and not self.purposes:
            return {"status": 200, "body": json.dumps(
                {"data": [{"id": n} for n in [*self.chat, *self.embedders]]})}
        if "/v1/models" in url:
            embedding = "purpose=embedding" in url
            names = self.embedders if embedding else self.chat
            purpose = "embedding" if embedding else "chat"
            return {"status": 200, "body": json.dumps(
                {"data": [{"id": n, "purpose": purpose} for n in names]})}
        if url.endswith(("/v1/embeddings", "/v1/chat/completions")) and self.call_status != 200:
            return {"status": self.call_status, "body": "upstream unavailable"}
        if url.endswith("/v1/embeddings"):
            return {"status": 200, "body": json.dumps(
                {"data": [{"embedding": [0.0] * self.dimensions, "index": 0}]})}
        if url.endswith("/v1/chat/completions"):
            return {"status": 200, "body": json.dumps(
                {"choices": [{"message": {"content": '{"ok": true}'}}]})}
        return {"status": 404, "body": ""}

    def __call__(self, argv, **kw):
        self.commands.append(list(argv))
        out = {}
        if "app.cli" in argv:
            out = self._cli(argv[argv.index("app.cli") + 1:])
        elif argv[:2] == ["docker", "run"]:
            source = kw.get("input") or ""
            literal = source.splitlines()[1][len("PAYLOAD = json.loads("):-1]
            out = self._http(json.loads(json.loads(literal)))
        return subprocess.CompletedProcess(argv, 0, json.dumps(out) if out else "", "")


def _ctx(tmp_path, monkeypatch, hive: _Hive, **given):
    monkeypatch.setattr(subprocess, "run", hive)
    inst = installer_at(tmp_path, answers(**given))
    ctx = steps.Ctx(inst, Shell(inst.home.log))
    return ctx


def _existing(tmp_path, monkeypatch, hive, **given):
    ctx = _ctx(tmp_path, monkeypatch, hive, modes={"proxy": "existing"}, proxy_url=EXISTING,
               **given)
    ctx.set_env(MIND_PROXY_KEY=OPERATOR_KEY)
    return ctx


# --- T5: an existing proxy is refused what it cannot serve ------------------


def test_a_classifier_the_proxy_does_not_list_is_forgotten_and_asked_again(tmp_path, monkeypatch):
    ctx = _existing(tmp_path, monkeypatch, _Hive(chat=["claude-sonnet-5"]),
                    embedding_model="qwen3-embedding:8b", classifier_model="gpt-oss:20b-32k")

    with pytest.raises(NeedInput) as asked:
        steps.resolve_models(ctx)
    assert asked.value.key == "classifier_model"
    assert asked.value.choices == ["claude-sonnet-5"]
    assert "does not offer the chat model 'gpt-oss:20b-32k'" in asked.value.reason
    assert "classifier_model" not in ctx.answers
    assert ctx.env().get(compose.CLASSIFIER_MODEL_VAR, "") == ""


def test_the_only_embedding_model_wider_than_4096_fails_the_step(tmp_path, monkeypatch):
    hive = _Hive(dimensions=8192)
    ctx = _existing(tmp_path, monkeypatch, hive,
                    embedding_model="qwen3-embedding:8b", classifier_model="gpt-oss:20b-32k")

    with pytest.raises(StepFailed, match=r"answered 8192 dimensions"):
        steps.resolve_models(ctx)
    embeds = [r for r in hive.requests if r["url"] == f"{EXISTING}/v1/embeddings"]
    assert [r["headers"]["Authorization"] for r in embeds] == [f"Bearer {OPERATOR_KEY}"]
    assert ctx.env().get(compose.EMBEDDING_MODEL_VAR, "") == ""


def test_a_narrower_embedding_model_is_accepted_and_given_to_lucent(tmp_path, monkeypatch):
    ctx = _existing(tmp_path, monkeypatch, _Hive(embedders=["text-embedding-3-large"],
                                                 dimensions=3072),
                    embedding_model="text-embedding-3-large", classifier_model="gpt-oss:20b-32k")

    steps.resolve_models(ctx)
    assert ctx.env()[compose.EMBEDDING_MODEL_VAR] == "text-embedding-3-large"


def test_models_an_existing_proxy_offers_and_answers_for_are_what_lucent_is_given(
        tmp_path, monkeypatch):
    hive = _Hive()
    ctx = _existing(tmp_path, monkeypatch, hive,
                    embedding_model="qwen3-embedding:8b", classifier_model="gpt-oss:20b-32k")

    steps.resolve_models(ctx)

    env = ctx.env()
    assert env[compose.EMBEDDING_MODEL_VAR] == "qwen3-embedding:8b"
    assert env[compose.CLASSIFIER_MODEL_VAR] == "gpt-oss:20b-32k"
    called = {r["url"]: r["headers"]["Authorization"] for r in hive.requests
              if r["method"] == "POST"}
    assert called == {f"{EXISTING}/v1/embeddings": f"Bearer {OPERATOR_KEY}",
                      f"{EXISTING}/v1/chat/completions": f"Bearer {OPERATOR_KEY}"}


# --- T7: a proxy the installer runs serves both models ----------------------


def _add_models(commands: list[list[str]]) -> list[list[str]]:
    return [c[c.index("app.cli") + 1:] for c in commands
            if "app.cli" in c and c[c.index("app.cli") + 1] == "add-model"]


def _flag(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def test_a_proxy_the_installer_runs_registers_the_embedding_model_and_the_classifier_first(
        tmp_path, monkeypatch):
    hive = _Hive()
    ctx = _ctx(tmp_path, monkeypatch, hive)

    steps.proxy_setup(ctx)

    added = [(_flag(a, "--provider"), _flag(a, "--model"), _flag(a, "--purpose"))
             for a in _add_models(hive.commands)]
    assert added[:2] == [(steps.LOCAL_PROVIDER, "qwen3-embedding:8b", "embedding"),
                         (steps.LOCAL_PROVIDER, "gpt-oss:20b-32k", "chat")]
    assert added[2][1] == "claude-sonnet-5"
    providers = [c[c.index("app.cli") + 1:] for c in hive.commands
                 if "app.cli" in c and c[c.index("app.cli") + 1] == "add-provider"]
    local = [p for p in providers if _flag(p, "--name") == steps.LOCAL_PROVIDER]
    assert [(_flag(p, "--kind"), _flag(p, "--base-url")) for p in local] == [
        ("ollama", compose.OLLAMA_URL)]
    assert ctx.env()["MIND_PROXY_KEY"] == "minted-key"


def test_the_hives_ollama_is_given_both_models_including_a_context_sized_tag(
        tmp_path, monkeypatch):
    hive = _Hive()
    ctx = _ctx(tmp_path, monkeypatch, hive)

    steps.embeddings(ctx)

    in_ollama = [c[c.index(compose.OLLAMA) + 1:] for c in hive.commands
                 if compose.OLLAMA in c]
    assert ["ollama", "pull", "qwen3-embedding:8b"] in in_ollama
    assert ["ollama", "pull", "gpt-oss:20b"] in in_ollama
    created = [c for c in in_ollama if c[:2] == ["sh", "-c"]]
    assert len(created) == 1
    assert f"PARAMETER num_ctx {32 * 1024}" in created[0][2]
    assert "ollama create gpt-oss:20b-32k" in created[0][2]


def test_a_proxy_that_marks_no_purposes_offers_all_its_models_and_the_pick_is_proved(
        tmp_path, monkeypatch):
    hive = _Hive(chat=["gpt-oss-120b"], embedders=["text-embedding-3-large"],
                 dimensions=3072, purposes=False)
    ctx = _existing(tmp_path, monkeypatch, hive)

    with pytest.raises(NeedInput) as asked:
        steps.resolve_models(ctx)
    assert set(asked.value.choices) == {"gpt-oss-120b", "text-embedding-3-large"}

    ctx.answers.update(embedding_model="text-embedding-3-large", classifier_model="gpt-oss-120b")
    steps.resolve_models(ctx)
    assert ctx.env()[compose.EMBEDDING_MODEL_VAR] == "text-embedding-3-large"
    assert ctx.env()[compose.CLASSIFIER_MODEL_VAR] == "gpt-oss-120b"


def test_an_embedding_pick_that_fails_its_call_is_asked_for_again(tmp_path, monkeypatch):
    hive = _Hive(chat=["gpt-oss-120b"], embedders=["text-embedding-3-large"],
                 dimensions=8192, purposes=False)
    ctx = _existing(tmp_path, monkeypatch, hive,
                    embedding_model="text-embedding-3-large", classifier_model="gpt-oss-120b")

    with pytest.raises(NeedInput) as asked:
        steps.resolve_models(ctx)
    assert asked.value.key == "embedding_model"
    assert "8192 dimensions" in asked.value.reason
    assert asked.value.choices == ["gpt-oss-120b"]
    assert ctx.env()[compose.EMBEDDING_MODEL_VAR] == ""


# --- a bad pick is asked for again in the same run --------------------------


def test_a_pick_the_proxy_does_not_list_is_forgotten_and_asked_again_in_the_same_run(
        tmp_path, monkeypatch):
    import time

    from installer import runner, secrets_store
    from installer.runner import Step

    hive = _Hive(embedders=["text-embedding-3-large"], dimensions=3072)
    monkeypatch.setattr(subprocess, "run", hive)
    inst = installer_at(tmp_path, answers(modes={"proxy": "existing"}, proxy_url=EXISTING),
                        step_list=[Step("models", "The hive's models", steps.resolve_models, 60)])
    ctx = steps.Ctx(inst, Shell(inst.home.log))
    # Saved by an earlier run: a pick this proxy does not list.
    ctx.set_env(MIND_PROXY_KEY=OPERATOR_KEY,
                **{compose.EMBEDDING_MODEL_VAR: "qwen3-embedding:8b"})
    inst.answers.update(embedding_model="qwen3-embedding:8b", classifier_model="gpt-oss:20b-32k")
    inst.save()

    run = __import__("threading").Thread(target=inst.runner.run, daemon=True)
    run.start()
    question = None
    for _ in range(200):
        step = (inst.runner.snapshot()["steps"] or [{"status": runner.PENDING}])[0]
        if step["status"] == runner.WAITING:
            question = step["question"]
            break
        assert step["status"] != runner.FAILED, step.get("error")
        time.sleep(0.05)

    assert question["key"] == "embedding_model"
    assert question["choices"] == ["text-embedding-3-large"]
    assert "does not offer" in question["reason"] and "qwen3-embedding:8b" in question["reason"]
    assert "embedding_model" not in json.loads(inst.home.answers.read_text())
    assert ctx.env()[compose.EMBEDDING_MODEL_VAR] == ""
    assert compose.EMBEDDING_MODEL_VAR not in secrets_store.parse_env(inst.home.secrets)

    assert inst.answer("embedding_model", "text-embedding-3-large") == []
    run.join(30)
    assert inst.runner.snapshot()["steps"][0]["status"] == runner.DONE
    assert ctx.env()[compose.EMBEDDING_MODEL_VAR] == "text-embedding-3-large"


def test_an_embedding_pick_whose_call_fails_is_asked_again_and_stays_a_choice(
        tmp_path, monkeypatch):
    ctx = _existing(tmp_path, monkeypatch,
                    _Hive(embedders=["qwen3-embedding:8b", "nomic-embed-text"], call_status=502),
                    embedding_model="qwen3-embedding:8b", classifier_model="gpt-oss:20b-32k")

    with pytest.raises(NeedInput) as asked:
        steps.resolve_models(ctx)
    assert asked.value.key == "embedding_model"
    assert "(502)" in asked.value.reason
    # One bad answer from the proxy does not take the hive's model off the list.
    assert asked.value.choices == ["qwen3-embedding:8b", "nomic-embed-text"]


def test_a_classifier_whose_call_fails_is_forgotten_and_asked_again_from_the_chat_models(
        tmp_path, monkeypatch):
    hive = _Hive()
    ctx = _existing(tmp_path, monkeypatch, hive,
                    embedding_model="qwen3-embedding:8b", classifier_model="gpt-oss:20b-32k")
    ctx.set_env(**{compose.CLASSIFIER_MODEL_VAR: "gpt-oss:20b-32k"})

    def failing_chat(payload, http=hive._http):
        if payload["url"].endswith("/v1/chat/completions"):
            return {"status": 500, "body": "the model crashed"}
        return http(payload)
    hive._http = failing_chat

    with pytest.raises(NeedInput) as asked:
        steps.resolve_models(ctx)
    assert asked.value.key == "classifier_model"
    assert "(500)" in asked.value.reason
    assert asked.value.choices == ["gpt-oss:20b-32k", "claude-sonnet-5"]
    assert "classifier_model" not in ctx.answers
    assert ctx.env()[compose.CLASSIFIER_MODEL_VAR] == ""
