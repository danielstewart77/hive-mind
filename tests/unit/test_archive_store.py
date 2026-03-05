"""Unit tests for the archive store abstraction (core.archive_store)."""

import json
from pathlib import Path


from core.archive_store import ArchivedEntry, ArchiveStore


def _make_entry(
    original_id: str = "4:abc:123",
    content: str = "Test content",
    data_class: str = "world-event",
    tags: str = "test",
    source: str = "user",
    agent_id: str = "ada",
    created_at: int = 1700000000,
    archived_at: str = "2026-03-01T00:00:00Z",
    original_metadata: dict | None = None,
) -> ArchivedEntry:
    return ArchivedEntry(
        original_id=original_id,
        content=content,
        data_class=data_class,
        tags=tags,
        source=source,
        agent_id=agent_id,
        created_at=created_at,
        archived_at=archived_at,
        original_metadata=original_metadata or {"tier": "T3"},
    )


class TestArchiveStoreSave:
    """Tests for ArchiveStore.save()."""

    def test_save_creates_file_if_not_exists(self, tmp_path: Path) -> None:
        """Saving to a non-existent file creates it and writes valid JSON."""
        store_path = tmp_path / "archive.json"
        store = ArchiveStore(store_path)
        entry = _make_entry()

        store.save(entry)

        assert store_path.exists()
        data = json.loads(store_path.read_text())
        assert isinstance(data, list)
        assert len(data) == 1

    def test_save_appends_to_existing_entries(self, tmp_path: Path) -> None:
        """Saving a second entry results in two entries in the file."""
        store_path = tmp_path / "archive.json"
        store = ArchiveStore(store_path)

        store.save(_make_entry(original_id="id-1", content="First"))
        store.save(_make_entry(original_id="id-2", content="Second"))

        data = json.loads(store_path.read_text())
        assert len(data) == 2
        assert data[0]["original_id"] == "id-1"
        assert data[1]["original_id"] == "id-2"

    def test_save_writes_correct_schema_fields(self, tmp_path: Path) -> None:
        """Saved JSON contains archived_at, original_id, content, data_class, original_metadata."""
        store_path = tmp_path / "archive.json"
        store = ArchiveStore(store_path)
        entry = _make_entry(
            original_id="test-id",
            content="World event content",
            data_class="world-event",
            archived_at="2026-03-01T12:00:00Z",
            original_metadata={"tier": "T3", "as_of": "2026-01-15"},
        )

        store.save(entry)

        data = json.loads(store_path.read_text())
        saved = data[0]
        assert saved["original_id"] == "test-id"
        assert saved["content"] == "World event content"
        assert saved["data_class"] == "world-event"
        assert saved["archived_at"] == "2026-03-01T12:00:00Z"
        assert saved["original_metadata"]["tier"] == "T3"
        assert saved["tags"] == "test"
        assert saved["source"] == "user"
        assert saved["agent_id"] == "ada"
        assert saved["created_at"] == 1700000000

    def test_save_handles_concurrent_write_gracefully(self, tmp_path: Path) -> None:
        """No data corruption on rapid sequential saves."""
        store_path = tmp_path / "archive.json"
        store = ArchiveStore(store_path)

        for i in range(20):
            store.save(_make_entry(original_id=f"id-{i}", content=f"Entry {i}"))

        data = json.loads(store_path.read_text())
        assert len(data) == 20
        ids = {entry["original_id"] for entry in data}
        assert len(ids) == 20


class TestArchiveStoreListAll:
    """Tests for ArchiveStore.list_all()."""

    def test_list_all_returns_saved_entries(self, tmp_path: Path) -> None:
        """list_all returns all previously saved entries."""
        store_path = tmp_path / "archive.json"
        store = ArchiveStore(store_path)
        store.save(_make_entry(original_id="id-1"))
        store.save(_make_entry(original_id="id-2"))

        entries = store.list_all()

        assert len(entries) == 2
        assert entries[0].original_id == "id-1"
        assert entries[1].original_id == "id-2"

    def test_list_all_empty_file_returns_empty_list(self, tmp_path: Path) -> None:
        """list_all returns [] when file is empty or missing."""
        store_path = tmp_path / "archive.json"
        store = ArchiveStore(store_path)

        entries = store.list_all()
        assert entries == []

    def test_list_all_returns_archived_entry_instances(self, tmp_path: Path) -> None:
        """list_all returns ArchivedEntry dataclass instances."""
        store_path = tmp_path / "archive.json"
        store = ArchiveStore(store_path)
        store.save(_make_entry())

        entries = store.list_all()
        assert isinstance(entries[0], ArchivedEntry)


class TestArchiveStoreGet:
    """Tests for ArchiveStore.get()."""

    def test_get_by_original_id_returns_entry(self, tmp_path: Path) -> None:
        """get(id) returns the matching archived entry."""
        store_path = tmp_path / "archive.json"
        store = ArchiveStore(store_path)
        store.save(_make_entry(original_id="target-id", content="Found me"))

        result = store.get("target-id")

        assert result is not None
        assert result.original_id == "target-id"
        assert result.content == "Found me"

    def test_get_by_original_id_not_found_returns_none(self, tmp_path: Path) -> None:
        """get(id) returns None for unknown ID."""
        store_path = tmp_path / "archive.json"
        store = ArchiveStore(store_path)
        store.save(_make_entry(original_id="other-id"))

        result = store.get("nonexistent-id")
        assert result is None

    def test_get_from_empty_store_returns_none(self, tmp_path: Path) -> None:
        """get() returns None when store is empty."""
        store_path = tmp_path / "archive.json"
        store = ArchiveStore(store_path)

        result = store.get("any-id")
        assert result is None
