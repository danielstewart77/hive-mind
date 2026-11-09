"""The dsh profile generator: what a dsh mind's launcher reads at boot."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from minds.harness import dsh_profile

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_profile_layers_the_hive_bundle_over_dsh_base(tmp_path):
    dsh_profile.write_profile("probe", tmp_path)

    manifest = json.loads((tmp_path / "probe/.dsh/profiles/hive/package.json").read_text())
    assert manifest["dsh"]["profile"]["bundles"] == [
        "@deepseek-ai/dsh-base",
        "@hive/dsh-headless-resumable",
    ]


def test_profile_links_the_bundle_and_hook_bridge_into_the_mounted_tree(tmp_path):
    dsh_profile.write_profile("probe", tmp_path, dsh_root="/srv/dsh/")

    modules = tmp_path / "probe/.dsh/profiles/hive/node_modules"
    assert os.readlink(modules / "@hive/dsh-headless-resumable") == (
        "/srv/dsh/hive/dsh-headless-resumable"
    )
    assert os.readlink(modules / "@deepseek-ai/dsh-hooks-claude-code") == (
        "/srv/dsh/packages/hooks/hooks-claude-code"
    )


def test_second_run_keeps_the_edited_patch_layer_and_repoints_a_stray_link(tmp_path):
    dsh_profile.write_profile("probe", tmp_path)
    profile = tmp_path / "probe/.dsh/profiles/hive"
    (profile / "cordis.patch.yml").write_text("- id: hmr\n  disabled: false\n")
    link = profile / "node_modules/@hive/dsh-headless-resumable"
    link.unlink()
    link.symlink_to("/elsewhere/dsh-headless-resumable")

    dsh_profile.write_profile("probe", tmp_path)

    assert (profile / "cordis.patch.yml").read_text() == "- id: hmr\n  disabled: false\n"
    assert os.readlink(link) == "/opt/dsh/hive/dsh-headless-resumable"


def test_module_runs_with_no_site_packages_and_reports_what_it_wrote(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    done = subprocess.run(
        [sys.executable, "-S", "-m", "minds.harness.dsh_profile",
         "--mind", "probe", "--out", str(tmp_path)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False,
    )

    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert report["profile_dir"] == str(tmp_path / "probe/.dsh/profiles/hive")
    assert report["dsh_home"] == str(tmp_path / "probe/.dsh")
    assert report["dsh_bin"] == "/opt/dsh/apps/cli/lib/bin.js"
    assert (tmp_path / "probe/.dsh/profiles/hive/cordis.yml").is_file()
