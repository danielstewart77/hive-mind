"""The settings panel: what a mind offers the console, and how a value lands.

The panel is generated from a declared schema rather than from the keys the
file happens to carry, and which controls appear is decided by the harness.
Both of those are behaviours, not conventions, so both are tested here.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from minds import runtime_api, runtime_settings

DSH_RUNTIME = """\
# Cypher's runtime configuration.
name: cypher
mind_id: 41984804-3ac1-4442-8f1b-606d8dd61d95
gateway_url: http://cypher:8420
description: "Cypher - dsh harness mind."

harness: dsh_cli
provider: ollama
# The model every new conversation starts on.
default_model: glm-5.3:cloud

# dsh's own serving ceiling, required at its boot — not the model's window.
context_window: 131072

# What a turn may not exceed.
turn_timeout_seconds: 14400
goal_rounds: 40
stop_on_failed_call: false

env:
  OPENAI_API_KEY: "invented-for-this-test"
"""

CLAUDE_RUNTIME = """\
name: ada
mind_id: 565e5a66-d20c-4266-872a-3268c4c894fc
gateway_url: http://ada:8420
description: Ada
harness: claude_cli
provider: anthropic
default_model: claude-sonnet-5
"""


@pytest.fixture()
def dsh_file(tmp_path):
    path = tmp_path / "runtime.yaml"
    path.write_text(DSH_RUNTIME)
    return path


@pytest.fixture()
def claude_file(tmp_path):
    path = tmp_path / "claude.yaml"
    path.write_text(CLAUDE_RUNTIME)
    return path


class TestWhichControlsAppear:
    def test_a_dsh_mind_is_offered_the_goal_and_timeout_controls(self, dsh_file):
        view = runtime_settings.settings_view(runtime_api.load_runtime(dsh_file))
        offered = {entry["key"] for entry in view["settings"]}
        assert {"turn_timeout_seconds", "goal_rounds", "stop_on_failed_call"} <= offered

    def test_a_claude_mind_is_offered_only_what_its_runner_honours(self, claude_file):
        """A control the runner ignores is worse than an absent one: the
        operator sets it and nothing happens, with no way to tell."""
        view = runtime_settings.settings_view(runtime_api.load_runtime(claude_file))
        offered = {entry["key"] for entry in view["settings"]}
        assert "description" in offered
        assert "rotation_threshold_percent" in offered
        assert offered.isdisjoint(
            {"turn_timeout_seconds", "goal_rounds", "stop_on_failed_call", "dsh_profile"}
        )

    def test_a_setting_the_file_lacks_is_still_offered_and_marked_absent(
        self, dsh_file
    ):
        view = runtime_settings.settings_view(runtime_api.load_runtime(dsh_file))
        entry = next(
            e for e in view["settings"] if e["key"] == "rotation_threshold_percent"
        )
        assert entry["present"] is False
        assert entry["value"] is None

    def test_the_payload_carries_only_declared_settings(self, dsh_file):
        """The file holds this mind's proxy key in its env block. The panel is
        built from the schema, so the payload is the settings and nothing
        else."""
        view = runtime_settings.settings_view(runtime_api.load_runtime(dsh_file))
        keys = {entry["key"] for entry in view["settings"]}
        assert keys <= set(runtime_settings.SETTINGS_BY_KEY)
        assert "env" not in keys
        assert "invented-for-this-test" not in str(view)


class TestWritingASetting:
    def test_a_number_lands_and_the_rest_of_the_file_survives(self, dsh_file):
        loaded = runtime_api.update_settings(dsh_file, {"turn_timeout_seconds": 3600})
        assert loaded["turn_timeout_seconds"] == 3600
        text = dsh_file.read_text()
        assert "# The model every new conversation starts on." in text
        assert "goal_rounds: 40" in text

    def test_zero_is_a_legal_timeout(self, dsh_file):
        """The control says zero means no bound, so zero has to be storable."""
        loaded = runtime_api.update_settings(dsh_file, {"turn_timeout_seconds": 0})
        assert loaded["turn_timeout_seconds"] == 0

    def test_a_setting_with_no_line_yet_is_created_with_its_comment(self, dsh_file):
        loaded = runtime_api.update_settings(
            dsh_file, {"rotation_threshold_percent": 30}
        )
        assert loaded["rotation_threshold_percent"] == 30
        assert "# How full the context window gets" in dsh_file.read_text()

    def test_a_false_switch_reads_back_as_false_and_not_as_a_string(self, dsh_file):
        """Python's `False` rendered with `str()` is the string "False", which
        a YAML reader loads as truthy — arming the thing just turned off."""
        loaded = runtime_api.update_settings(dsh_file, {"stop_on_failed_call": False})
        assert loaded["stop_on_failed_call"] is False

    def test_a_true_switch_reads_back_as_a_boolean(self, dsh_file):
        loaded = runtime_api.update_settings(dsh_file, {"stop_on_failed_call": True})
        assert loaded["stop_on_failed_call"] is True

    def test_a_description_holding_a_colon_still_parses(self, dsh_file):
        """A bare colon splits the line into a nested mapping, and "Cypher:
        the dsh mind" is the ordinary thing an operator types."""
        sentence = "Cypher: the dsh mind, local models #1"
        loaded = runtime_api.update_settings(dsh_file, {"description": sentence})
        assert loaded["description"] == sentence

    def test_a_word_where_a_number_belongs_leaves_the_file_untouched(self, dsh_file):
        before = dsh_file.read_text()
        with pytest.raises(ValueError):
            runtime_api.update_settings(dsh_file, {"goal_rounds": "soon"})
        assert dsh_file.read_text() == before

    def test_a_number_out_of_range_leaves_the_file_untouched(self, dsh_file):
        before = dsh_file.read_text()
        with pytest.raises(ValueError):
            runtime_api.update_settings(dsh_file, {"rotation_threshold_percent": 150})
        assert dsh_file.read_text() == before

    def test_one_bad_value_refuses_the_whole_save(self, dsh_file):
        """Rendering happens before any write, so a save never half-lands."""
        with pytest.raises(ValueError):
            runtime_api.update_settings(
                dsh_file, {"goal_rounds": 4, "turn_timeout_seconds": "later"}
            )
        assert runtime_api.load_runtime(dsh_file)["goal_rounds"] == 40

    def test_a_setting_this_harness_does_not_honour_is_refused(self, claude_file):
        with pytest.raises(ValueError):
            runtime_api.update_settings(claude_file, {"goal_rounds": 4})

    def test_a_description_cannot_smuggle_a_second_line(self, dsh_file):
        with pytest.raises(ValueError):
            runtime_api.update_settings(
                dsh_file, {"description": "fine\ngoal_rounds: 999"}
            )
        assert runtime_api.load_runtime(dsh_file)["goal_rounds"] == 40


@pytest.fixture()
def client(dsh_file, monkeypatch):
    monkeypatch.setenv("MIND_ADMIN_TOKEN", "s3cret")
    app = FastAPI()
    runtime_api.install_runtime_routes(
        app, path=dsh_file, mind_id="mind-1", log=logging.getLogger("test")
    )
    return TestClient(app, raise_server_exceptions=False)


class TestRoutes:
    def test_the_panel_is_admin_guarded(self, client):
        """The listing names every setting on a port reachable across the LAN."""
        assert client.get("/runtime/settings").status_code == 401

    def test_the_panel_reports_the_current_values(self, client):
        body = client.get(
            "/runtime/settings", headers={"Authorization": "Bearer s3cret"}
        ).json()
        entry = next(e for e in body["settings"] if e["key"] == "goal_rounds")
        assert entry["value"] == 40
        assert body["harness"] == "dsh_cli"

    def test_a_save_writes_the_file_and_reports_the_new_value(self, client, dsh_file):
        response = client.patch(
            "/runtime/settings",
            json={"settings": {"goal_rounds": 1}},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert response.status_code == 200
        assert runtime_api.load_runtime(dsh_file)["goal_rounds"] == 1
        entry = next(
            e for e in response.json()["settings"] if e["key"] == "goal_rounds"
        )
        assert entry["value"] == 1

    def test_a_refused_value_answers_400_and_changes_nothing(self, client, dsh_file):
        response = client.patch(
            "/runtime/settings",
            json={"settings": {"goal_rounds": 0}},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert response.status_code == 400
        assert runtime_api.load_runtime(dsh_file)["goal_rounds"] == 40

    def test_a_save_without_a_credential_is_refused(self, client, dsh_file):
        assert client.patch(
            "/runtime/settings", json={"settings": {"goal_rounds": 1}}
        ).status_code == 401
        assert runtime_api.load_runtime(dsh_file)["goal_rounds"] == 40


class TestTheCachedContextWindow:
    """The window belongs to the model, so it is read from the proxy and
    written beside the model — never typed, and never offered as a setting.

    What needs it is a per-turn hook sizing a rotation threshold from a
    percentage, and a hook that made a network call to do that would pay for
    the proxy on every turn.
    """

    def test_it_is_not_one_of_the_settings_the_panel_offers(self, dsh_file):
        view = runtime_settings.settings_view(runtime_api.load_runtime(dsh_file))
        offered = {entry["key"] for entry in view["settings"]}
        assert runtime_settings.CONTEXT_WINDOW_FIELD not in offered

    def test_the_panel_reports_it_beside_the_settings(self, dsh_file):
        runtime_api.update_runtime_fields(dsh_file, {"model_context_window": "131072"})
        view = runtime_settings.settings_view(runtime_api.load_runtime(dsh_file))
        assert view["context_window"] == 131072

    def test_a_model_save_caches_the_window_the_proxy_declared(
        self, client, dsh_file, monkeypatch
    ):
        async def catalog(_path):
            return [
                {"name": "glm-5.3:cloud", "context_window": 1_048_576},
                {"name": "other", "context_window": 1024},
            ]

        from minds import models_api

        monkeypatch.setattr(models_api, "build_catalog", catalog)
        response = client.patch(
            "/runtime",
            json={"default_model": "glm-5.3:cloud"},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert response.status_code == 200
        assert runtime_api.load_runtime(dsh_file)["model_context_window"] == 1_048_576

    def test_a_model_whose_window_nobody_declared_clears_the_previous_one(
        self, client, dsh_file, monkeypatch
    ):
        """A stale window is worse than none. The rotation hook multiplies a
        percentage by it, so keeping the old model's figure after a move to an
        unmeasured one rotates the conversation at room it does not have."""
        runtime_api.update_runtime_fields(dsh_file, {"model_context_window": "131072"})

        async def catalog(_path):
            return [{"name": "mystery", "context_window": None}]

        from minds import models_api

        monkeypatch.setattr(models_api, "build_catalog", catalog)
        response = client.patch(
            "/runtime",
            json={"default_model": "mystery"},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert response.status_code == 200
        assert runtime_api.load_runtime(dsh_file)["model_context_window"] == 0
        view = runtime_settings.settings_view(runtime_api.load_runtime(dsh_file))
        assert view["context_window"] is None

    def test_a_caller_cannot_write_a_window_of_its_own(self, client, dsh_file):
        """The window is a measurement the proxy makes, and the rotation
        threshold is derived from it. One taken off a request body is a number
        nobody measured."""
        runtime_api.update_runtime_fields(dsh_file, {"model_context_window": "131072"})
        response = client.patch(
            "/runtime",
            json={"model_context_window": "4096"},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert response.status_code == 400
        assert runtime_api.load_runtime(dsh_file)["model_context_window"] == 131072


class TestAValueThatWouldBreakTheFile:
    def test_an_escape_sequence_in_a_description_is_refused(self, dsh_file):
        """Pasted out of a coloured terminal log is how one arrives, and YAML
        refuses to parse a document holding one — so a write that let it
        through would leave a mind whose every boot and turn fails on a file
        nobody edited."""
        before = dsh_file.read_text()
        with pytest.raises(ValueError):
            runtime_api.update_settings(
                dsh_file, {"description": "Cypher \x1b[31mred\x1b[0m mind"}
            )
        assert dsh_file.read_text() == before
        assert runtime_api.load_runtime(dsh_file)["name"] == "cypher"

    def test_a_document_that_would_not_parse_never_replaces_the_file(
        self, dsh_file, monkeypatch
    ):
        """The guard is before `os.replace`, not after. Validating afterwards
        reports a refused write from a route that has already committed the
        breakage."""
        monkeypatch.setattr(
            runtime_settings, "render_value", lambda key, value: '"unterminated'
        )
        before = dsh_file.read_text()
        with pytest.raises(ValueError):
            runtime_api.update_settings(dsh_file, {"goal_rounds": 4})
        assert dsh_file.read_text() == before


class TestAMindMissingALine:
    """Most minds declare only some of these settings, so the partial save is
    the ordinary case rather than the edge one.

    Bob's file has no `dsh_profile` line and no `description`. A save that
    carried every control's value would carry two empty strings with it, a
    blank text value is refused, and the refusal is the whole write — so he
    would be unable to change any setting at all from the panel that exists to
    replace editing his file by hand.
    """

    @pytest.fixture()
    def sparse_file(self, tmp_path):
        path = tmp_path / "sparse.yaml"
        path.write_text(
            "name: bob\n"
            "mind_id: b9f2dc0c-e0c5-466f-b6ab-30ed1c0f0001\n"
            "harness: dsh_cli\n"
            "provider: ollama\n"
            "default_model: nemotron-3-super:cloud\n"
        )
        return path

    def test_one_setting_saves_on_a_file_declaring_none_of_them(self, sparse_file):
        loaded = runtime_api.update_settings(sparse_file, {"goal_rounds": 5})
        assert loaded["goal_rounds"] == 5
        assert loaded["default_model"] == "nemotron-3-super:cloud"

    def test_a_blank_text_value_is_refused_rather_than_written(self, sparse_file):
        """Which is why the page must not send a control it never filled in."""
        with pytest.raises(ValueError):
            runtime_api.update_settings(sparse_file, {"description": ""})
        assert "description" not in runtime_api.load_runtime(sparse_file)


class TestASubstitutionThatWouldNotMeanWhatItSays:
    """One-line substitution cannot express every way YAML states a value, and
    the dangerous cases parse cleanly — so the written document is read back
    and compared against what was asked for."""

    def test_a_folded_description_is_refused_rather_than_half_replaced(
        self, tmp_path
    ):
        """A `>` block keeps its continuation lines, which attach to the new
        scalar. Valid YAML, saying something nobody typed."""
        path = tmp_path / "folded.yaml"
        path.write_text(
            "name: cypher\n"
            "harness: dsh_cli\n"
            "description: >\n"
            "  a long thing\n"
            "  over two lines\n"
        )
        before = path.read_text()
        with pytest.raises(ValueError):
            runtime_api.update_settings(path, {"description": "replaced"})
        assert path.read_text() == before

    def test_a_duplicated_key_is_refused_rather_than_reported_saved(self, tmp_path):
        """Substitution replaces the first occurrence and YAML reads the last,
        so the save would report success and change nothing."""
        path = tmp_path / "dupe.yaml"
        path.write_text(
            "name: cypher\nharness: dsh_cli\ngoal_rounds: 1\ngoal_rounds: 40\n"
        )
        with pytest.raises(ValueError):
            runtime_api.update_settings(path, {"goal_rounds": 9})
        assert runtime_api.load_runtime(path)["goal_rounds"] == 40

    def test_a_save_keeps_the_files_own_mode(self, dsh_file):
        """A temp file is 0600. A mind's config going unreadable to everyone
        but its owner is a read that starts failing with no edit to explain
        it."""
        dsh_file.chmod(0o644)
        runtime_api.update_settings(dsh_file, {"goal_rounds": 7})
        assert dsh_file.stat().st_mode & 0o777 == 0o644


class TestTheDshServingCeilingIsNotTheModelsWindow:
    """`context_window` is dsh's own: the ceiling its profile declares, which
    its adapter requires at boot and its compaction reads. The model's own
    window is a different number from a different source, and one key holding
    both is how a model save stops a mind starting."""

    def test_the_cached_window_does_not_touch_the_dsh_ceiling(
        self, client, dsh_file, monkeypatch
    ):
        async def catalog(_path):
            return [{"name": "glm-5.3:cloud", "context_window": 1_048_576}]

        from minds import models_api

        monkeypatch.setattr(models_api, "build_catalog", catalog)
        client.patch(
            "/runtime",
            json={"default_model": "glm-5.3:cloud"},
            headers={"Authorization": "Bearer s3cret"},
        )
        loaded = runtime_api.load_runtime(dsh_file)
        assert loaded["context_window"] == 131072
        assert loaded["model_context_window"] == 1_048_576

    def test_the_ceiling_is_not_offered_as_a_setting(self, dsh_file):
        """It is read once at the adapter's import and required to be
        non-zero; a control implying otherwise would be a box whose value
        does nothing until a restart, and breaks the mind if it is wrong."""
        view = runtime_settings.settings_view(runtime_api.load_runtime(dsh_file))
        assert "context_window" not in {e["key"] for e in view["settings"]}
