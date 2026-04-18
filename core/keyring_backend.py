"""Custom keyring backend that reads storage path from KEY_RING env var.

Subclasses keyrings.alt.file.PlaintextKeyring so the storage location is
controlled by KEY_RING directly — no XDG_DATA_HOME overloading, no import-
order bridge. Select it via PYTHON_KEYRING_BACKEND=core.keyring_backend.HiveMindKeyring.
"""

import os

from keyrings.alt.file import PlaintextKeyring


class HiveMindKeyring(PlaintextKeyring):
    @property
    def file_path(self) -> str:
        root = os.environ.get("KEY_RING")
        if root:
            return os.path.join(root, "python_keyring", "keyring_pass.cfg")
        return super().file_path
