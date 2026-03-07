"""Unit tests for core.memory_schema — data class registry and validation."""

import logging
from datetime import datetime, timezone

import pytest

from core.memory_schema import (
    DATA_CLASS_REGISTRY,
    VALID_SOURCES,
    VALID_TIERS,
    DataClassDef,
    build_metadata,
    validate_data_class,
    validate_source,
)


class TestDataClassRegistry:
    """Tests for the DATA_CLASS_REGISTRY constant."""

    def test_registry_contains_all_classes(self) -> None:
        expected = {
            "technical-config",
            "timed-event",
            "person",
            "news-event",
            "preference",
            "intention",
            "ada-identity",
            "future-project",
            "ephemeral",
            "news-digest",
        }
        assert set(DATA_CLASS_REGISTRY.keys()) == expected
        assert len(DATA_CLASS_REGISTRY) == 10

    def test_registry_technical_config_is_reviewable(self) -> None:
        dc = DATA_CLASS_REGISTRY["technical-config"]
        assert dc.tier == "reviewable"
        assert "reviewable" in dc.tags
        assert "technical" in dc.tags

    def test_registry_person_is_durable(self) -> None:
        dc = DATA_CLASS_REGISTRY["person"]
        assert dc.tier == "durable"
        assert "durable" in dc.tags
        assert "person" in dc.tags

    def test_registry_timed_event_requires_expires(self) -> None:
        dc = DATA_CLASS_REGISTRY["timed-event"]
        assert dc.requires_expires is True
        # All other classes should NOT require expires
        for name, cls_def in DATA_CLASS_REGISTRY.items():
            if name != "timed-event":
                assert cls_def.requires_expires is False, (
                    f"{name} should not require expires_at"
                )

    def test_registry_entries_are_frozen_dataclasses(self) -> None:
        for name, dc in DATA_CLASS_REGISTRY.items():
            assert isinstance(dc, DataClassDef)
            with pytest.raises(AttributeError):
                dc.name = "modified"  # type: ignore[misc]

    def test_valid_sources_contains_expected(self) -> None:
        assert VALID_SOURCES == {"user", "tool", "session", "self"}

    def test_valid_tiers_contains_expected(self) -> None:
        assert VALID_TIERS == {"reviewable", "durable"}


class TestValidateDataClass:
    """Tests for validate_data_class()."""

    def test_validate_data_class_known_returns_def(self) -> None:
        result = validate_data_class("person")
        assert result is not None
        assert result.name == "person"
        assert result.tier == "durable"

    def test_validate_data_class_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown-class"):
            validate_data_class("unknown-class")

    def test_validate_data_class_none_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="required"):
            validate_data_class(None)

    def test_validate_data_class_empty_string_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_data_class("")


class TestValidateSource:
    """Tests for validate_source()."""

    def test_validate_source_valid(self) -> None:
        for src in VALID_SOURCES:
            result = validate_source(src)
            assert result == src

    def test_validate_source_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="random"):
            validate_source("random")


class TestBuildMetadata:
    """Tests for build_metadata()."""

    def test_build_metadata_with_data_class(self) -> None:
        meta = build_metadata(data_class="person", source="user")
        assert meta["data_class"] == "person"
        assert meta["tier"] == "durable"
        assert meta["source"] == "user"
        assert meta["superseded"] is False
        assert "as_of" in meta
        assert "expires_at" not in meta or meta.get("expires_at") is None

    def test_build_metadata_timed_event_requires_expires_at(self) -> None:
        with pytest.raises(ValueError, match="expires_at"):
            build_metadata(data_class="timed-event", source="user")

    def test_build_metadata_timed_event_with_expires_at(self) -> None:
        meta = build_metadata(
            data_class="timed-event",
            source="user",
            expires_at="2026-04-01T00:00:00Z",
        )
        assert meta["expires_at"] == "2026-04-01T00:00:00Z"
        assert meta["data_class"] == "timed-event"
        assert meta["tier"] == "reviewable"

    def test_build_metadata_without_data_class_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="required"):
            build_metadata(data_class=None, source="user")

    def test_build_metadata_as_of_defaults_to_now(self) -> None:
        before = datetime.now(timezone.utc).isoformat()
        meta = build_metadata(data_class="person", source="user")
        after = datetime.now(timezone.utc).isoformat()
        assert before <= meta["as_of"] <= after

    def test_build_metadata_as_of_custom(self) -> None:
        custom = "2025-01-15T12:00:00Z"
        meta = build_metadata(data_class="person", source="user", as_of=custom)
        assert meta["as_of"] == custom

    def test_build_metadata_invalid_source_raises(self) -> None:
        with pytest.raises(ValueError, match="random"):
            build_metadata(data_class="person", source="random")
