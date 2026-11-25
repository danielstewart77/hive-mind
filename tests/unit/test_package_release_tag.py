"""A release tag can never name a version the package's files do not hold."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("build_package_release", ROOT / "scripts" / "build-package-release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def _package(tmp_path, version):
    folder = tmp_path / "modules" / "hive-surfaces"
    folder.mkdir(parents=True)
    (folder / "pyproject.toml").write_text(f'[project]\nname = "hive-surfaces"\nversion = "{version}"\n')
    return folder


def test_a_tag_equal_to_pyproject_names_the_package_folder(tmp_path):
    folder = _package(tmp_path, "1.4.2")
    assert release.check_tag("hive-surfaces-v1.4.2", tmp_path) == folder


def test_a_tag_that_differs_from_pyproject_is_refused(tmp_path):
    _package(tmp_path, "1.4.2")
    with pytest.raises(SystemExit, match="1.4.3.*1.4.2"):
        release.check_tag("hive-surfaces-v1.4.3", tmp_path)


def test_the_real_surfaces_package_is_released_under_its_own_number():
    version = release.package_version("hive-surfaces")
    assert release.check_tag(f"hive-surfaces-v{version}") == ROOT / "modules" / "hive-surfaces"
