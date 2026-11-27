"""The signed-in person's own key, read from the key folder. Read only.

The hive site makes and keeps a person's key (a file under the key root, in
the `.people` folder). This site only reads it, to send it to the gateway as
that person's own credential. It never makes, replaces or records a key; with
no key file there is nothing to send and the caller says so.

The file name rule is the hive site's (`mind_key_files.person_stem`): every
character other than an ASCII letter or digit becomes its own hex code.
"""

import os
from pathlib import Path

PEOPLE_FOLDER = ".people"
FILE_NAME = "key"  # same name the hive site's mind_key_files.FILE_NAME gives
NO_KEY_SENTENCE = (
    "You have no key yet. Open the hive site once so it makes your key, "
    "then come back here."
)


def key_root() -> Path:
    return Path(os.environ.get("HIVE_KEY_ROOT", "/mind_keys"))


def person_stem(username: str) -> str:
    name = str(username or "").strip()
    if not name:
        return ""
    return "".join(c.upper() if c.isalnum() and c.isascii() else f"_{ord(c):X}_" for c in name)


def read_person_key(username: str) -> str | None:
    """The person's key, or None when they have none or it cannot be read."""
    stem = person_stem(username)
    if not stem:
        return None
    try:
        text = (key_root() / PEOPLE_FOLDER / stem / FILE_NAME).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return text.strip() or None
