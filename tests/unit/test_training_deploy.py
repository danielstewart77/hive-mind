"""Unit tests for turning a trained adapter into a served model.

Nothing here talks to a real Ollama or starts a real container. What is
worth testing is the decision layer: what counts as deployable, that the
merge is the only route and Ollama the only quantizer, and that every
failure comes back as a structured refusal the console can render rather
than an exception.
"""

from __future__ import annotations

from io import BytesIO

import pytest

from core import training_deploy
from core.training_deploy import (
    MERGED_GGUF_NAME,
    DeployRequest,
    adapter_weight_file,
    deploy,
    merged_gguf_path,
    render_modelfile,
)


@pytest.fixture
def adapter_dir(tmp_path):
    directory = tmp_path / "run" / "adapter"
    directory.mkdir(parents=True)
    (directory / "adapter_model.safetensors").write_bytes(b"weights")
    (directory / "adapter_config.json").write_text("{}")
    return directory


def _request(adapter_dir, **kwargs):
    return DeployRequest(
        model_name=kwargs.pop("model_name", "skippy-lora"),
        adapter_dir=str(adapter_dir),
        serve_tag=kwargs.pop("serve_tag", "qwen3:8b"),
        **kwargs,
    )


# ------------------------------------------------------------- validation


def test_a_complete_request_validates(adapter_dir):
    assert _request(adapter_dir).validate() == []


def test_a_missing_adapter_directory_is_a_problem(tmp_path):
    request = DeployRequest(
        model_name="x", adapter_dir=str(tmp_path / "nope"), serve_tag="qwen3:8b"
    )
    assert any("not found" in problem for problem in request.validate())


def test_a_directory_without_weights_is_a_problem(tmp_path):
    empty = tmp_path / "adapter"
    empty.mkdir()
    request = DeployRequest(
        model_name="x", adapter_dir=str(empty), serve_tag="qwen3:8b"
    )
    assert any("no adapter weights" in problem for problem in request.validate())


def test_the_retired_adapter_strategy_is_refused_as_unknown(adapter_dir):
    """Requirement 1: merge is the only route the API accepts.

    Ollama can only stack a LoRA onto the architectures its own converter
    understands, which excludes what this hive trains. Silently promoting
    the request to a merge would turn one click into an hour of GPU and a
    full model of disk without anyone asking for it.
    """
    request = _request(adapter_dir, strategy="adapter")
    assert any("unknown strategy" in problem for problem in request.validate())


def test_the_serve_tag_is_required(adapter_dir):
    request = _request(adapter_dir, serve_tag="")
    assert any("serve_tag" in problem for problem in request.validate())


def test_adapter_weight_file_finds_either_supported_name(tmp_path):
    directory = tmp_path / "a"
    directory.mkdir()
    (directory / "adapter_model.bin").write_bytes(b"x")
    assert adapter_weight_file(directory).name == "adapter_model.bin"


# --------------------------------------------------------------- modelfile


def test_the_modelfile_names_the_merged_file_and_its_parameters(adapter_dir):
    rendered = render_modelfile(_request(adapter_dir), MERGED_GGUF_NAME)
    assert f"FROM {MERGED_GGUF_NAME}" in rendered
    assert "PARAMETER num_ctx 32768" in rendered


def test_a_system_prompt_reaches_the_modelfile(adapter_dir):
    rendered = render_modelfile(
        _request(adapter_dir, system_prompt="You are Skippy."), "qwen3:8b"
    )
    assert 'SYSTEM """You are Skippy."""' in rendered


# ----------------------------------------------------------- the merge path


class _FakeResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def test_validation_failures_never_touch_the_network(tmp_path, monkeypatch):
    monkeypatch.setattr(
        training_deploy,
        "_post_json",
        lambda *a, **k: pytest.fail("an invalid request reached Ollama"),
    )
    result = deploy(
        DeployRequest(
            model_name="x", adapter_dir=str(tmp_path / "gone"), serve_tag="qwen3:8b"
        )
    )
    assert result.stage == "refused"


def test_merge_launches_a_conversion_when_no_gguf_exists_yet(
    adapter_dir, monkeypatch
):
    commands = []

    class _Completed:
        stdout = "container123\n"

    monkeypatch.setattr(
        training_deploy.subprocess,
        "run",
        lambda cmd, **kw: (commands.append(cmd), _Completed())[1],
    )
    result = deploy(
        _request(adapter_dir, base_model="Qwen/Qwen3-8B"),
        ollama_url="http://ollama.test",
    )

    assert result.deployed is False
    assert result.stage == "converting"
    assert result.container_id == "container123"
    assert "--mode" in commands[0] and "merge" in commands[0]
    assert "--gpus" in commands[0]


def test_the_merge_container_is_told_nothing_about_quantization(
    adapter_dir, monkeypatch
):
    """Requirement 5: the target type is chosen at import, not at merge.

    Passing it here is what made changing q4 to q8 cost another two-hour
    merge instead of another upload — and is the argument the retired
    quantizer read.
    """
    commands = []

    class _Completed:
        stdout = "container123\n"

    monkeypatch.setattr(
        training_deploy.subprocess,
        "run",
        lambda cmd, **kw: (commands.append(list(cmd)), _Completed())[1],
    )
    result = deploy(
        _request(adapter_dir, quantization="q8_0", base_model="Qwen/Qwen3-8B"),
        ollama_url="http://ollama.test",
    )

    assert result.stage == "converting"
    assert "q8_0" not in commands[0]
    assert "--quantization" not in commands[0]


def test_merge_serves_the_gguf_once_the_conversion_has_produced_one(
    adapter_dir, monkeypatch
):
    gguf = merged_gguf_path(adapter_dir)
    gguf.write_bytes(b"gguf")
    seen = {}
    monkeypatch.setattr(
        training_deploy, "upload_blob", lambda path, url, **kw: "sha256:def"
    )

    def fake_post(url, payload, timeout):
        seen["payload"] = payload
        return {"status": "success"}

    monkeypatch.setattr(training_deploy, "_post_json", fake_post)
    monkeypatch.setattr(
        training_deploy.subprocess,
        "run",
        lambda *a, **k: pytest.fail("a second conversion was launched"),
    )

    result = deploy(
        _request(adapter_dir), ollama_url="http://ollama.test"
    )

    assert result.deployed is True
    assert seen["payload"]["files"] == {MERGED_GGUF_NAME: "sha256:def"}
    assert "from" not in seen["payload"]


def test_ollama_is_handed_the_quantization_to_apply_on_import(
    adapter_dir, monkeypatch
):
    """Requirement 3: the uploaded file is f16 and Ollama shrinks it."""
    gguf = merged_gguf_path(adapter_dir)
    gguf.write_bytes(b"gguf")
    seen = {}
    monkeypatch.setattr(
        training_deploy, "upload_blob", lambda path, url, **kw: "sha256:def"
    )
    monkeypatch.setattr(
        training_deploy,
        "_post_json",
        lambda url, payload, timeout: seen.update(payload) or {"status": "success"},
    )

    result = deploy(
        _request(adapter_dir, quantization="q5_K_M"),
        ollama_url="http://ollama.test",
    )

    assert result.deployed is True
    assert seen["quantize"] == "q5_K_M"


def test_a_refused_quantization_is_reported_and_nothing_is_claimed_served(
    adapter_dir, monkeypatch
):
    """Requirement 4: Ollama saying no is a refusal, not an exception.

    Ollama rejects a quantization type it does not implement, and a deploy
    that swallowed that would leave the console announcing a served model
    that Ollama never created.
    """
    gguf = merged_gguf_path(adapter_dir)
    gguf.write_bytes(b"gguf")
    monkeypatch.setattr(
        training_deploy, "upload_blob", lambda path, url, **kw: "sha256:def"
    )
    monkeypatch.setattr(
        training_deploy,
        "_post_json",
        lambda *a, **k: {"error": "unsupported quantization type q3_XS"},
    )

    result = deploy(
        _request(adapter_dir, quantization="q3_XS"), ollama_url="http://ollama.test"
    )

    assert result.deployed is False
    assert result.stage == "refused"
    assert "unsupported quantization type" in result.blockers[0]


def test_an_unreachable_ollama_is_a_refusal(adapter_dir, monkeypatch):
    gguf = merged_gguf_path(adapter_dir)
    gguf.write_bytes(b"gguf")
    monkeypatch.setattr(
        training_deploy, "upload_blob", lambda path, url, **kw: "sha256:def"
    )

    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(training_deploy, "_post_json", boom)
    result = deploy(_request(adapter_dir), ollama_url="http://ollama.test")

    assert result.deployed is False
    assert "connection refused" in result.blockers[0]


def test_a_failed_upload_never_reaches_create(adapter_dir, monkeypatch):
    gguf = merged_gguf_path(adapter_dir)
    gguf.write_bytes(b"gguf")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(training_deploy, "upload_blob", boom)
    monkeypatch.setattr(
        training_deploy,
        "_post_json",
        lambda *a, **k: pytest.fail("create was called after a failed upload"),
    )
    result = deploy(_request(adapter_dir), ollama_url="http://ollama.test")

    assert result.deployed is False


def test_a_docker_failure_during_merge_is_a_refusal(adapter_dir, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("docker socket missing")

    monkeypatch.setattr(training_deploy.subprocess, "run", boom)
    result = deploy(
        _request(adapter_dir), ollama_url="http://ollama.test"
    )
    assert result.stage == "refused"
    assert "docker socket missing" in result.blockers[0]


def test_the_merged_gguf_sits_beside_the_adapter_not_inside_it(adapter_dir):
    """Inside would make it look like part of the adapter to every reader."""
    assert merged_gguf_path(adapter_dir).parent == adapter_dir.parent


# --------------------------------------------------------------- blob push


def test_a_blob_already_present_is_not_re_uploaded(tmp_path, monkeypatch):
    blob = tmp_path / "adapter_model.safetensors"
    blob.write_bytes(b"weights")
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.get_method())
        return _FakeResponse(b"")

    monkeypatch.setattr(training_deploy.urllib.request, "urlopen", fake_urlopen)
    digest = training_deploy.upload_blob(blob, "http://ollama.test")

    assert digest.startswith("sha256:")
    assert calls == ["HEAD"]


def test_a_missing_blob_is_uploaded(tmp_path, monkeypatch):
    blob = tmp_path / "adapter_model.safetensors"
    blob.write_bytes(b"weights")
    calls = []

    def fake_urlopen(request, timeout=None):
        method = request.get_method()
        calls.append(method)
        if method == "HEAD":
            raise training_deploy.urllib.error.HTTPError(
                "http://ollama.test", 404, "not found", {}, None
            )
        return _FakeResponse(b"")

    monkeypatch.setattr(training_deploy.urllib.request, "urlopen", fake_urlopen)
    training_deploy.upload_blob(blob, "http://ollama.test")

    assert calls == ["HEAD", "POST"]


def test_the_digest_is_the_content_hash(tmp_path, monkeypatch):
    import hashlib

    blob = tmp_path / "adapter_model.safetensors"
    blob.write_bytes(b"weights")
    monkeypatch.setattr(
        training_deploy.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse(b""),
    )
    expected = hashlib.sha256(b"weights").hexdigest()
    assert training_deploy.upload_blob(blob, "http://ollama.test") == f"sha256:{expected}"
