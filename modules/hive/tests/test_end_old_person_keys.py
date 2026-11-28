"""The one-time step of story 914: every person's key made the old way ends in
the one record, its file is deleted, each person is named, and a second run is
safe. A mind's key and its folder are untouched. Real, against the scratch
Postgres and a temporary key folder.
"""
import hashlib
import time

import key_migration
import mind_key_files
from hive_site import testing

from .mind_key_support import a_mind_with_a_key, a_person_with_a_key

OLD_DAN = "dan-old-person-key-0123456789"  # secret-guard: allow
OLD_SAM = "sam-old-person-key-0123456789"  # secret-guard: allow
MIND_KEY = "ada-mind-own-key-0123456789"  # secret-guard: allow


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _known(value):
    from hive_data import core
    return testing.run(core.credential_for_token(_sha(value)))


def _plant(root, stem, value):
    folder = root / ".people" / stem
    folder.mkdir(parents=True)
    (folder / "key").write_text(value + "\n")


def test_the_one_time_step_ends_each_old_person_key_deletes_the_files_and_leaves_minds_alone(
        builtin_console, tmp_path, monkeypatch):
    from hive_data import core

    root = tmp_path / "keys"
    (root / "ada").mkdir(parents=True)
    (root / "ada" / "key").write_text(MIND_KEY + "\n")
    monkeypatch.setattr(mind_key_files, "KEY_ROOT", root)
    a_person_with_a_key(OLD_DAN, "dan", admin=True)
    a_person_with_a_key(OLD_SAM, "sam", admin=False)
    a_mind_with_a_key(MIND_KEY, "ada")
    _plant(root, "DAN", OLD_DAN)
    _plant(root, "SAM", OLD_SAM)
    testing.run(core.record_sign_in(
        username="sam", token_sha=_sha("sam-browser-sign-in-0123456789"),  # secret-guard: allow
        ends_at=time.time() + 3600))
    assert _known(OLD_DAN) and _known(OLD_SAM) and _known(MIND_KEY)

    first = testing.run(key_migration.end_old_person_keys())

    assert first["ended_people"] == ["dan", "sam"]
    assert sorted(first["key_files_deleted_for"]) == ["DAN", "SAM"]
    assert _known(OLD_DAN) is None and _known(OLD_SAM) is None
    assert _known("sam-browser-sign-in-0123456789") is None  # secret-guard: allow
    assert not (root / ".people").exists()
    assert (root / "ada" / "key").read_text().strip() == MIND_KEY
    assert _known(MIND_KEY) is not None

    second = testing.run(key_migration.end_old_person_keys())

    assert second["ended_people"] == [] and second["key_files_deleted_for"] == []
    assert _known(MIND_KEY) is not None
    assert (root / "ada" / "key").read_text().strip() == MIND_KEY
