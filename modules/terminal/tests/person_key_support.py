"""Give a test person a key file in a scratch key folder, laid out the way
the hive site lays it out (`.people/<STEM>/key`)."""

import os

import person_key

TEST_KEY = "test-person-key-0123456789abcdefghijklmnop"


def give_key(tmp_path, monkeypatch, username="daniel", value=TEST_KEY):
    monkeypatch.setenv("HIVE_KEY_ROOT", str(tmp_path))
    folder = tmp_path / person_key.PEOPLE_FOLDER / person_key.person_stem(username)
    os.makedirs(folder, exist_ok=True)
    (folder / person_key.FILE_NAME).write_text(value + "\n", encoding="utf-8")
    return value
