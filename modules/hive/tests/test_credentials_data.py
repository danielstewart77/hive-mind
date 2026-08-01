"""Unit tests for credentials_data.py's env-file credential fan-out."""

import os
import sys
import time
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import credentials_data


def _write_env(path, **kv):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"{k}={v}" for k, v in kv.items()) + "\n")


def _patch_env_files(monkeypatch, tmp_path, **files):
    """files: label -> Path. Overrides only the given labels; others point nowhere."""
    env_files = {label: tmp_path / f"missing-{label}.env" for label in credentials_data.ENV_FILES}
    env_files.update(files)
    monkeypatch.setattr(credentials_data, "ENV_FILES", env_files)
    monkeypatch.setattr(credentials_data, "BACKUP_DIR", tmp_path / "backups")


def test_list_credentials_reports_in_sync_shared_secret(tmp_path, monkeypatch):
    nervous_system = tmp_path / "nervous_system.env"
    hive_mind = tmp_path / "hive_mind.env"
    _write_env(nervous_system, COMMS_BEARER_TOKEN="same-value", COMMS_ADMIN_BEARER_TOKEN="x")
    _write_env(hive_mind, COMMS_BEARER_TOKEN="same-value")
    _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous_system, hive_mind=hive_mind)

    rows = credentials_data.list_credentials()
    comms = next(r for r in rows if r["key"] == "comms_bearer_token")

    present = [loc for loc in comms["locations"] if loc["present"]]
    assert len(present) == 2
    assert comms["in_sync"] is True
    assert all("same-value" not in str(loc["preview"]) for loc in present)


def test_list_credentials_flags_out_of_sync(tmp_path, monkeypatch):
    nervous_system = tmp_path / "nervous_system.env"
    hive_mind = tmp_path / "hive_mind.env"
    _write_env(nervous_system, COMMS_BEARER_TOKEN="value-a")
    _write_env(hive_mind, COMMS_BEARER_TOKEN="value-b")
    _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous_system, hive_mind=hive_mind)

    rows = credentials_data.list_credentials()
    comms = next(r for r in rows if r["key"] == "comms_bearer_token")

    assert comms["in_sync"] is False


def test_list_credentials_marks_manual_only_entries_non_rotatable():
    rows = credentials_data.list_credentials()
    non_rotatable = {r["key"] for r in rows if not r["rotatable"]}

    assert "hive_tools_token" in non_rotatable
    assert "github_pat" in non_rotatable
    # Manual-only entries explain themselves; settable ones offer a set flow instead.
    assert all(
        r["manual_reason"] or r["settable"] for r in rows if not r["rotatable"]
    )


def test_rotate_credential_writes_new_value_to_every_location(tmp_path, monkeypatch):
    nervous_system = tmp_path / "nervous_system.env"
    hive_mind = tmp_path / "hive_mind.env"
    skippy = tmp_path / "skippy.env"
    _write_env(nervous_system, COMMS_BEARER_TOKEN="old", OTHER_KEY="untouched")
    _write_env(hive_mind, COMMS_BEARER_TOKEN="old")
    _write_env(skippy, COMMS_BEARER_TOKEN="old")
    _patch_env_files(
        monkeypatch, tmp_path,
        nervous_system=nervous_system, hive_mind=hive_mind, skippy=skippy,
    )

    result = credentials_data.rotate_credential("comms_bearer_token")

    new_comms = credentials_data._read_env_value(nervous_system, "COMMS_BEARER_TOKEN")
    assert new_comms != "old"
    assert credentials_data._read_env_value(hive_mind, "COMMS_BEARER_TOKEN") == new_comms
    assert credentials_data._read_env_value(skippy, "COMMS_BEARER_TOKEN") == new_comms
    assert credentials_data._read_env_value(nervous_system, "OTHER_KEY") == "untouched"
    assert len(result["written"]) == 3
    assert result["restart_targets"]


def test_rotate_credential_skips_files_missing_the_key(tmp_path, monkeypatch):
    nervous_system = tmp_path / "nervous_system.env"
    _write_env(nervous_system, COMMS_BEARER_TOKEN="old")
    _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous_system)

    result = credentials_data.rotate_credential("comms_bearer_token")

    assert result["written"] == [{"file": "nervous_system", "env_key": "COMMS_BEARER_TOKEN"}]


def test_rotate_credential_rejects_manual_only_key():
    with pytest.raises(ValueError, match="sqlite"):
        credentials_data.rotate_credential("hive_tools_token")


def test_rotate_credential_rejects_unknown_key():
    with pytest.raises(ValueError, match="unknown credential"):
        credentials_data.rotate_credential("not_a_real_key")


def test_write_env_value_preserves_inode_and_permissions(tmp_path, monkeypatch):
    """The .env files are single-file bind mounts: replacing the inode breaks the mount."""
    monkeypatch.setattr(credentials_data, "BACKUP_DIR", tmp_path / "backups")
    path = tmp_path / "secret.env"
    _write_env(path, TOKEN="old")
    path.chmod(0o600)
    inode_before = path.stat().st_ino

    assert credentials_data._write_env_value(path, "TOKEN", "new") is True
    assert credentials_data._read_env_value(path, "TOKEN") == "new"
    assert path.stat().st_ino == inode_before
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_write_env_value_backs_up_the_original_before_rewriting(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    monkeypatch.setattr(credentials_data, "BACKUP_DIR", backups)
    path = tmp_path / "secret.env"
    _write_env(path, TOKEN="old", OTHER="keep")

    credentials_data._write_env_value(path, "TOKEN", "new", label="skippy")

    written = list(backups.iterdir())
    assert len(written) == 1
    assert written[0].name.startswith("skippy-TOKEN-")
    assert "TOKEN=old" in written[0].read_text()
    assert "OTHER=keep" in written[0].read_text()


def _patch_github_files(monkeypatch, tmp_path, git_credentials=None, gh_hosts=None):
    monkeypatch.setattr(credentials_data, "BACKUP_DIR", tmp_path / "backups")
    creds = tmp_path / ".git-credentials"
    hosts = tmp_path / "hosts.yml"
    if git_credentials is not None:
        creds.write_text(git_credentials)
    if gh_hosts is not None:
        hosts.write_text(gh_hosts)
    monkeypatch.setattr(credentials_data, "GIT_CREDENTIALS_PATH", creds)
    monkeypatch.setattr(credentials_data, "GH_HOSTS_PATH", hosts)
    return creds, hosts


_GIT_CREDENTIALS = "https://daniel:ghp_oldtoken@github.com\nhttps://x:y@huggingface.co\n"  # secret-guard: allow — invented fixture
_GH_HOSTS = (
    "github.com:\n"
    "    git_protocol: https\n"
    "    users:\n"
    "        daniel:\n"
    "            oauth_token: ghp_oldtoken\n"  # secret-guard: allow — invented fixture
    "    user: daniel\n"
    "    oauth_token: ghp_oldtoken\n"  # secret-guard: allow — invented fixture
)


def test_github_pat_status_reads_both_files(tmp_path, monkeypatch):
    _patch_github_files(monkeypatch, tmp_path, _GIT_CREDENTIALS, _GH_HOSTS)

    rows = credentials_data.list_credentials()
    pat = next(r for r in rows if r["key"] == "github_pat")

    assert pat["settable"] is True
    assert pat["rotatable"] is False
    assert pat["in_sync"] is True
    by_file = {loc["file"]: loc for loc in pat["locations"]}
    assert by_file["git_credentials"]["present"] is True
    assert by_file["git_credentials"]["preview"] == "...oken"
    assert by_file["gh_hosts"]["present"] is True
    assert by_file["gh_hosts"]["env_key"] == "oauth_token x2"


def test_github_pat_status_flags_out_of_sync(tmp_path, monkeypatch):
    _patch_github_files(
        monkeypatch, tmp_path,
        "https://daniel:ghp_different@github.com\n", _GH_HOSTS,  # secret-guard: allow — invented fixture
    )

    pat = next(r for r in credentials_data.list_credentials() if r["key"] == "github_pat")

    assert pat["in_sync"] is False


def test_set_github_pat_verifies_then_writes_both_files(tmp_path, monkeypatch):
    creds, hosts = _patch_github_files(monkeypatch, tmp_path, _GIT_CREDENTIALS, _GH_HOSTS)
    monkeypatch.setattr(credentials_data, "_verify_github_pat", lambda token: "danielstewart77")  # secret-guard: allow — invented fixture

    result = credentials_data.set_github_pat("ghp_newtoken")

    assert result["login"] == "danielstewart77"
    assert "https://daniel:ghp_newtoken@github.com" in creds.read_text()  # secret-guard: allow — invented fixture
    assert "https://x:y@huggingface.co" in creds.read_text()  # secret-guard: allow — invented fixture
    assert creds.read_text().count("ghp_newtoken") == 1
    assert hosts.read_text().count("oauth_token: ghp_newtoken") == 2
    assert "ghp_oldtoken" not in hosts.read_text()
    assert {w["file"] for w in result["written"]} == {"git_credentials", "gh_hosts"}
    backups = sorted(f.name for f in (tmp_path / "backups").iterdir())
    assert any(name.startswith("git_credentials-") for name in backups)
    assert any(name.startswith("gh_hosts-") for name in backups)


def test_set_github_pat_rejects_bad_token_before_writing(tmp_path, monkeypatch):
    creds, hosts = _patch_github_files(monkeypatch, tmp_path, _GIT_CREDENTIALS, _GH_HOSTS)

    def _reject(token):
        raise ValueError("GitHub rejected that token (401) — nothing was written")

    monkeypatch.setattr(credentials_data, "_verify_github_pat", _reject)

    with pytest.raises(ValueError, match="401"):
        credentials_data.set_github_pat("ghp_bogus")
    assert "ghp_oldtoken" in creds.read_text()
    assert "ghp_oldtoken" in hosts.read_text()


def test_set_github_pat_rejects_garbage_without_calling_github(tmp_path, monkeypatch):
    _patch_github_files(monkeypatch, tmp_path, _GIT_CREDENTIALS, _GH_HOSTS)

    def _boom(token):
        raise AssertionError("should not verify garbage")

    monkeypatch.setattr(credentials_data, "_verify_github_pat", _boom)

    with pytest.raises(ValueError, match="look like a token"):
        credentials_data.set_github_pat("  ")
    with pytest.raises(ValueError, match="look like a token"):
        credentials_data.set_github_pat("ghp_has space")


def test_set_github_pat_errors_when_no_file_has_an_entry(tmp_path, monkeypatch):
    _patch_github_files(monkeypatch, tmp_path)  # neither file exists
    monkeypatch.setattr(credentials_data, "_verify_github_pat", lambda token: "someone")

    with pytest.raises(ValueError, match="neither credential file"):
        credentials_data.set_github_pat("ghp_new")


def test_comms_bearer_note_distinguishes_it_from_the_admin_token():
    comms = next(
        r for r in credentials_data.list_credentials() if r["key"] == "comms_bearer_token"
    )
    assert "separate credential" in comms["note"]


def test_write_env_value_leaves_file_untouched_when_key_absent(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    monkeypatch.setattr(credentials_data, "BACKUP_DIR", backups)
    path = tmp_path / "secret.env"
    _write_env(path, OTHER="keep")

    assert credentials_data._write_env_value(path, "TOKEN", "new") is False
    assert path.read_text() == "OTHER=keep\n"
    assert not backups.exists()


# --- Per-location rotation reporting -----------------------------------
#
# A credential lives in several files. Writing it is not one operation: it is
# one atomic action per file, and the result has to say so. The defect these
# cover is a rotation that wrote 4 of 5 files and reported success, leaving
# one consumer holding a token the rest of the hive had stopped honouring.


def test_one_unwritable_location_does_not_stop_the_others(tmp_path, monkeypatch):
    """Requirement 1 — each file is written on its own."""
    # Declared order is nervous_system, hive_mind, skippy, mordecai, hive_tools.
    # The two failure classes that survive the _unwritable_reason pre-check sit
    # in that order with a writable file after each: hive_mind holds a non-UTF-8
    # byte, mordecai refuses the write. Both fail inside _write_location rather
    # than before it, which is the only way to exercise its handlers — a test
    # driving the pre-check alone (a directory, a missing file) leaves them
    # dead, and the decode failure is not an OSError, so it escapes a guard
    # written for one and not the other.
    nervous_system = tmp_path / "nervous_system.env"
    skippy = tmp_path / "skippy.env"
    hive_tools = tmp_path / "hive_tools.env"
    hive_mind = tmp_path / "hive_mind.env"
    mordecai = tmp_path / "mordecai.env"
    _write_env(nervous_system, COMMS_BEARER_TOKEN="old")
    _write_env(skippy, COMMS_BEARER_TOKEN="old")
    _write_env(hive_tools, COMMS_BEARER_TOKEN="old")
    hive_mind.write_bytes(b"# caf\xe9 latin-1 comment\nCOMMS_BEARER_TOKEN=old\n")
    _write_env(mordecai, COMMS_BEARER_TOKEN="old")
    mordecai.chmod(0o400)
    _patch_env_files(
        monkeypatch, tmp_path,
        nervous_system=nervous_system, skippy=skippy, hive_tools=hive_tools,
        hive_mind=hive_mind, mordecai=mordecai,
    )

    result = credentials_data.rotate_credential("comms_bearer_token")

    rotated = credentials_data._read_env_value(nervous_system, "COMMS_BEARER_TOKEN")
    assert rotated != "old"
    # The files *after* each failing one in the declared order are written too —
    # a failure that aborted the loop would leave these on the old value.
    assert credentials_data._read_env_value(skippy, "COMMS_BEARER_TOKEN") == rotated
    assert credentials_data._read_env_value(hive_tools, "COMMS_BEARER_TOKEN") == rotated
    assert credentials_data._read_env_value(mordecai, "COMMS_BEARER_TOKEN") == "old"
    assert hive_mind.read_bytes().endswith(b"COMMS_BEARER_TOKEN=old\n")
    by_file = {row["file"]: row for row in result["locations"]}
    assert by_file["nervous_system"]["written"] is True
    assert by_file["skippy"]["written"] is True
    assert by_file["hive_tools"]["written"] is True
    assert by_file["hive_mind"]["written"] is False
    assert by_file["mordecai"]["written"] is False


def test_the_result_reports_every_declared_location(tmp_path, monkeypatch):
    """Requirement 2 — one row per location, each saying whether it was written."""
    nervous_system = tmp_path / "nervous_system.env"
    _write_env(nervous_system, COMMS_BEARER_TOKEN="old")
    _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous_system)

    result = credentials_data.rotate_credential("comms_bearer_token")

    # Literal, not derived from the group being tested: the comms token is
    # declared in these five files and a row is owed for each of them.
    assert [row["file"] for row in result["locations"]] == [
        "nervous_system", "hive_mind", "skippy", "mordecai", "hive_tools",
    ]
    assert all("written" in row for row in result["locations"])
    assert sum(row["written"] for row in result["locations"]) == 1


def test_an_unwritten_location_reports_the_reason_that_applies(tmp_path, monkeypatch):
    """Requirement 3 — directory, missing file and absent key read differently."""
    nervous_system = tmp_path / "nervous_system.env"
    hive_tools = tmp_path / "hive_tools_env_dir"
    mordecai = tmp_path / "mordecai.env"
    _write_env(nervous_system, COMMS_BEARER_TOKEN="old")
    hive_tools.mkdir()
    _write_env(mordecai, SOMETHING_ELSE="x")
    _patch_env_files(
        monkeypatch, tmp_path,
        nervous_system=nervous_system, hive_tools=hive_tools, mordecai=mordecai,
    )

    result = credentials_data.rotate_credential("comms_bearer_token")
    by_file = {row["file"]: row for row in result["locations"]}

    assert by_file["nervous_system"]["reason"] is None
    assert "directory" in by_file["hive_tools"]["reason"]
    assert str(hive_tools) in by_file["hive_tools"]["reason"]
    # skippy's path was never created by the fixture at all.
    assert "no file" in by_file["skippy"]["reason"]
    assert "COMMS_BEARER_TOKEN" in by_file["mordecai"]["reason"]
    # Three different failures must not collapse into one house message.
    assert len({
        by_file["hive_tools"]["reason"],
        by_file["skippy"]["reason"],
        by_file["mordecai"]["reason"],
    }) == 3


def test_a_rotation_that_missed_a_location_is_not_reported_as_complete(
    tmp_path, monkeypatch
):
    """Requirement 4 — partial is distinguishable from success."""
    nervous_system = tmp_path / "nervous_system.env"
    _write_env(nervous_system, COMMS_BEARER_TOKEN="old")
    _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous_system)

    partial = credentials_data.rotate_credential("comms_bearer_token")
    assert partial["complete"] is False

    # And it is not simply always False: every location present reports complete.
    everywhere = {
        label: tmp_path / f"{label}.env"
        for label in ("nervous_system", "hive_mind", "skippy", "mordecai", "hive_tools")
    }
    for path in everywhere.values():
        _write_env(path, COMMS_BEARER_TOKEN="old")
    _patch_env_files(monkeypatch, tmp_path, **everywhere)

    assert credentials_data.rotate_credential("comms_bearer_token")["complete"] is True


def test_setting_the_github_token_reports_each_of_its_files(tmp_path, monkeypatch):
    """Requirement 5 — the GitHub token reports per location like the rest."""
    creds, hosts = _patch_github_files(monkeypatch, tmp_path, _GIT_CREDENTIALS, None)
    monkeypatch.setattr(credentials_data, "_verify_github_pat", lambda token: "daniel")

    result = credentials_data.set_github_pat("ghp_newtoken")  # secret-guard: allow — invented fixture

    by_file = {row["file"]: row for row in result["locations"]}
    assert set(by_file) == {"git_credentials", "gh_hosts"}
    assert by_file["git_credentials"]["written"] is True
    assert by_file["git_credentials"]["reason"] is None
    assert by_file["gh_hosts"]["written"] is False
    assert "no file" in by_file["gh_hosts"]["reason"]
    assert result["complete"] is False
    assert not hosts.exists()


def test_a_rotation_that_wrote_nothing_is_a_failure(tmp_path, monkeypatch):
    """Requirement 7 — nothing written is an error, not a partial success."""
    hive_tools = tmp_path / "hive_tools_env_dir"
    hive_tools.mkdir()
    _patch_env_files(monkeypatch, tmp_path, hive_tools=hive_tools)

    with pytest.raises(ValueError) as raised:
        credentials_data.rotate_credential("comms_bearer_token")

    # The failure carries the same per-location detail the success does,
    # otherwise the operator is told it failed and not which files to fix.
    assert "hive_tools" in str(raised.value)
    assert "directory" in str(raised.value)

    # And the quieter shape of the same failure: every file present, readable
    # and writable, none of them carrying the key. Nothing refuses the write,
    # so this is the case a "did any write throw?" check would call a success.
    for label in credentials_data.ENV_FILES:
        _write_env(tmp_path / f"present-{label}.env", SOMETHING_ELSE="x")
    _patch_env_files(
        monkeypatch, tmp_path,
        **{label: tmp_path / f"present-{label}.env" for label in credentials_data.ENV_FILES},
    )

    with pytest.raises(ValueError) as absent:
        credentials_data.rotate_credential("comms_bearer_token")

    assert "is not in this file" in str(absent.value)


def test_two_rotations_at_once_cannot_lose_one_anothers_writes(tmp_path, monkeypatch):
    """Requirement 8 — concurrent rotations serialise, and neither over-reports."""
    import threading

    # One file holds both credentials, which is the real shape: four of the
    # five comms/lucent tokens live in nervous-system/.env together.
    nervous_system = tmp_path / "nervous_system.env"
    _write_env(nervous_system, COMMS_BEARER_TOKEN="old-comms", LUCENT_BEARER_TOKEN="old-lucent")
    _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous_system)

    # Widen the read-modify-write window so an unserialised pair reliably
    # interleaves: without this the race is real but rarely observed.
    original_rewrite = credentials_data._rewrite_in_place

    def slow_rewrite(path, updated, original):
        time.sleep(0.05)
        original_rewrite(path, updated, original)

    monkeypatch.setattr(credentials_data, "_rewrite_in_place", slow_rewrite)

    results: dict[str, Any] = {}
    threads = [
        threading.Thread(
            target=lambda key=key: results.__setitem__(
                key, credentials_data.rotate_credential(key)
            )
        )
        for key in ("comms_bearer_token", "lucent_bearer_token")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Each rotation claimed this file; the file has to agree with both claims.
    # Unserialised, the second writer's copy of the original silently drops the
    # first writer's key change, and both still report it written.
    for key, env_key, was in (
        ("comms_bearer_token", "COMMS_BEARER_TOKEN", "old-comms"),
        ("lucent_bearer_token", "LUCENT_BEARER_TOKEN", "old-lucent"),
    ):
        row = next(
            location for location in results[key]["locations"]
            if location["file"] == "nervous_system"
        )
        assert row["written"] is True
        assert credentials_data._read_env_value(nervous_system, env_key) != was


def test_a_duplicated_key_rotates_the_copy_the_service_loads(tmp_path, monkeypatch):
    """Requirement 9 — every occurrence is rotated, not just the first."""
    nervous_system = tmp_path / "nervous_system.env"
    # A stale duplicate below a live one: what a hand-edit leaves behind, and
    # the lower line is the one compose and python-dotenv actually load.
    nervous_system.write_text(
        "COMMS_BEARER_TOKEN=old-first\nOTHER=x\nCOMMS_BEARER_TOKEN=old-last\n"  # secret-guard: allow — invented fixture
    )
    _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous_system)

    result = credentials_data.rotate_credential("comms_bearer_token")

    lines = [
        line for line in nervous_system.read_text().splitlines()
        if line.startswith("COMMS_BEARER_TOKEN=")
    ]
    assert len(lines) == 2
    assert len(set(lines)) == 1, "both copies must carry the rotated value"
    assert "old-first" not in nervous_system.read_text()
    assert "old-last" not in nervous_system.read_text()
    # And the console reads back what the service would load.
    assert result["locations"][0]["written"] is True
    assert credentials_data._read_env_value(
        nervous_system, "COMMS_BEARER_TOKEN"
    ) == lines[0].split("=", 1)[1]


def test_a_write_that_fails_partway_leaves_the_file_as_it_was(tmp_path, monkeypatch):
    """Requirement 10 — a failed write never leaves the file empty."""
    nervous_system = tmp_path / "nervous_system.env"
    _write_env(nervous_system, COMMS_BEARER_TOKEN="old", OTHER_KEY="keep-me")
    before = nervous_system.read_text()
    _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous_system)

    # Fail at the fsync, after open(path, "w") has already truncated. ENOSPC,
    # an EIO on the bind mount, a remount taken mid-write all land here.
    real_fsync = os.fsync
    calls = {"n": 0}

    def failing_fsync(fd):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(28, "No space left on device")
        return real_fsync(fd)

    monkeypatch.setattr(credentials_data.os, "fsync", failing_fsync)

    with pytest.raises(ValueError) as raised:
        credentials_data.rotate_credential("comms_bearer_token")

    assert nervous_system.read_text() == before, "the original contents must be restored"
    assert "could not be written" in str(raised.value)


def test_setting_the_pat_leaves_other_hosts_tokens_alone(tmp_path, monkeypatch):
    """Requirement 11 — only the github.com block is rewritten."""
    hosts = tmp_path / "hosts.yml"
    hosts.write_text(
        "github.com:\n"
        "    users:\n"
        "        danielstewart77:\n"
        "            oauth_token: gho_old\n"
        "    user: danielstewart77\n"
        "    oauth_token: gho_old\n"
        "ghe.corp.example.com:\n"
        "    users:\n"
        "        danielstewart77:\n"
        "            oauth_token: ghe_ENTERPRISE_SECRET\n"  # secret-guard: allow — invented fixture
        "    oauth_token: ghe_ENTERPRISE_SECRET\n"  # secret-guard: allow — invented fixture
    )
    git_credentials = tmp_path / ".git-credentials"
    git_credentials.write_text("https://danielstewart77:gho_old@github.com\n")  # secret-guard: allow — invented fixture
    monkeypatch.setattr(credentials_data, "GH_HOSTS_PATH", hosts)
    monkeypatch.setattr(credentials_data, "GIT_CREDENTIALS_PATH", git_credentials)
    monkeypatch.setattr(credentials_data, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(credentials_data, "_verify_github_pat", lambda token: "danielstewart77")  # secret-guard: allow — a login, not a secret

    credentials_data.set_github_pat("ghp_brandnew")

    text = hosts.read_text()
    assert text.count("ghp_brandnew") == 2, "both github.com entries take the new token"
    assert text.count("ghe_ENTERPRISE_SECRET") == 2, "the enterprise host is untouched"


def test_one_unreadable_file_does_not_hide_the_other_credentials(tmp_path, monkeypatch):
    """Requirement 12 — the listing survives a file it cannot read."""
    nervous_system = tmp_path / "nervous_system.env"
    hive_mind = tmp_path / "hive_mind.env"
    _write_env(nervous_system, COMMS_BEARER_TOKEN="live-value")
    hive_mind.write_bytes(b"COMMS_BEARER_TOKEN=caf\xe9\n")
    _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous_system, hive_mind=hive_mind)

    rows = credentials_data.list_credentials()

    assert len(rows) == len(credentials_data.CREDENTIAL_GROUPS)
    comms = next(row for row in rows if row["key"] == "comms_bearer_token")
    by_file = {loc["file"]: loc for loc in comms["locations"]}
    assert by_file["hive_mind"]["unreadable"] is True
    assert by_file["hive_mind"]["present"] is False
    # The file it *can* read is still reported, rather than lost with the tab.
    assert by_file["nervous_system"]["present"] is True


def test_an_empty_value_is_not_reported_as_a_configured_credential(tmp_path, monkeypatch):
    """Requirement 13 — KEY= is not set, and a hive of blanks is not in sync."""
    nervous_system = tmp_path / "nervous_system.env"
    hive_mind = tmp_path / "hive_mind.env"
    _write_env(nervous_system, COMMS_BEARER_TOKEN="")
    _write_env(hive_mind, COMMS_BEARER_TOKEN="")
    _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous_system, hive_mind=hive_mind)

    comms = next(
        row for row in credentials_data.list_credentials() if row["key"] == "comms_bearer_token"
    )

    by_file = {loc["file"]: loc for loc in comms["locations"]}
    assert by_file["nervous_system"]["present"] is False
    assert not by_file["nervous_system"]["preview"]
    # Every file agreeing on nothing is not agreement.
    assert comms["in_sync"] is False


def test_a_backup_is_owner_only_and_never_overwrites_an_earlier_one(tmp_path, monkeypatch):
    """Requirement 15 — 0600 from creation, one file per rotation."""
    import stat

    nervous_system = tmp_path / "nervous_system.env"
    _write_env(nervous_system, COMMS_BEARER_TOKEN="old")
    backups = tmp_path / "backups"
    _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous_system)
    monkeypatch.setattr(credentials_data, "BACKUP_DIR", backups)
    # Same second for both rotations: the stamp alone cannot separate them.
    monkeypatch.setattr(credentials_data.time, "strftime", lambda *a, **k: "20260801T090000Z")

    credentials_data.rotate_credential("comms_bearer_token")
    credentials_data.rotate_credential("comms_bearer_token")

    written = sorted(backups.iterdir())
    assert len(written) == 2, "the second rotation must not overwrite the first's copy"
    for backup in written:
        mode = stat.S_IMODE(backup.stat().st_mode)
        assert mode == 0o600, f"{backup.name} is {oct(mode)}"


def test_a_token_that_cannot_be_sent_as_a_header_is_refused_clearly(tmp_path, monkeypatch):
    """Requirement 16 — a bad paste is a message, not a server error."""
    reached = {"verify": False}

    def _fail(token):
        reached["verify"] = True
        return "nobody"

    monkeypatch.setattr(credentials_data, "_verify_github_pat", _fail)

    # A zero-width space survives .strip() and is not .isspace(), so it used to
    # reach urllib and raise UnicodeEncodeError while building the header.
    with pytest.raises(ValueError) as raised:
        credentials_data.set_github_pat("ghp_abc​def")

    assert "doesn't look like a token" in str(raised.value)
    assert reached["verify"] is False, "nothing should be sent to GitHub"
