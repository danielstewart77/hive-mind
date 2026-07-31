"""Unit tests for the base-model catalog.

The catalog is data the console renders directly, so the tests are about
its integrity — every entry has to be usable, and the annotations that tell
someone whether a model is trainable and servable today have to reflect the
host rather than the wish.
"""

from __future__ import annotations

import json
from io import BytesIO

import pytest

from core import training_models
from core.training_models import (
    ARCH_DENSE,
    ARCH_MOE,
    CATALOG,
    DEFAULT_BASE_MODEL_ID,
    catalog,
    get,
    installed_serve_tags,
)


def test_every_entry_carries_what_the_console_renders():
    for model in CATALOG:
        assert "/" in model.id, f"{model.id} is not a HuggingFace repo id"
        assert model.label
        assert model.serve_tag
        assert model.architecture in {ARCH_DENSE, ARCH_MOE}
        assert model.parameters_b > 0
        assert model.train_vram_mib > 0
        assert len(model.note) > 80, f"{model.id} needs a real explanation"


def test_exactly_one_model_is_recommended():
    assert sum(1 for model in CATALOG if model.recommended) == 1
    assert get(DEFAULT_BASE_MODEL_ID) is not None


def test_the_recommended_default_is_dense():
    """A first fine-tune should not also be someone's first MoE debugging."""
    assert get(DEFAULT_BASE_MODEL_ID).architecture == ARCH_DENSE


def test_ids_are_unique():
    ids = [model.id for model in CATALOG]
    assert len(ids) == len(set(ids))


def test_an_unknown_id_resolves_to_nothing():
    assert get("acme/does-not-exist") is None


def test_vram_estimates_rise_with_parameter_count():
    """A bigger model that claimed to be cheaper would misrank the list."""
    ordered = sorted(CATALOG, key=lambda m: m.parameters_b)
    estimates = [m.train_vram_mib for m in ordered]
    assert estimates == sorted(estimates)


class _FakeResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def test_installed_tags_come_from_ollama(monkeypatch):
    payload = {"models": [{"name": "qwen3:8b"}, {"name": "gpt-oss:20b"}]}
    monkeypatch.setattr(
        training_models.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse(json.dumps(payload).encode()),
    )
    assert installed_serve_tags("http://ollama.test") == {"qwen3:8b", "gpt-oss:20b"}


def test_an_unreachable_ollama_is_an_empty_set_not_an_error(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(training_models.urllib.request, "urlopen", boom)
    assert installed_serve_tags("http://ollama.test") == set()


def test_catalog_annotates_which_tags_are_pulled(monkeypatch):
    monkeypatch.setattr(
        training_models, "installed_serve_tags", lambda url=None: {"qwen3:8b"}
    )
    rows = {row["id"]: row for row in catalog()}
    assert rows["Qwen/Qwen3-8B"]["serve_tag_installed"] is True
    assert rows["Qwen/Qwen3-4B-Instruct-2507"]["serve_tag_installed"] is False


def test_catalog_reports_what_fits_in_the_free_vram(monkeypatch):
    monkeypatch.setattr(training_models, "installed_serve_tags", lambda url=None: set())
    rows = {row["id"]: row for row in catalog(free_vram_mib=16_000)}
    assert rows["Qwen/Qwen3-8B"]["fits_now"] is True
    assert rows["Qwen/Qwen3-30B-A3B-Instruct-2507"]["fits_now"] is False


def test_fit_is_unknown_rather_than_false_without_a_gpu_reading(monkeypatch):
    """No reading is not the same as no room, and the console says so."""
    monkeypatch.setattr(training_models, "installed_serve_tags", lambda url=None: set())
    assert all(row["fits_now"] is None for row in catalog(free_vram_mib=None))


@pytest.mark.parametrize("model", CATALOG, ids=lambda m: m.id)
def test_moe_entries_say_so_in_their_note(model):
    """The MoE caveat is the one thing a reader cannot infer from the name."""
    if model.architecture == ARCH_MOE:
        assert "experts" in model.note.lower() or "mixture-of-experts" in model.note.lower()
