"""Unit tests for core.backfill_classifier — classification engine for backfill."""

import pytest

from core.backfill_classifier import ClassificationResult, classify_entry, classify_entity_node


class TestClassificationResultDataclass:
    """Tests for the ClassificationResult dataclass structure."""

    def test_classification_result_dataclass_fields(self) -> None:
        result = ClassificationResult(
            data_class="person", confidence=0.9, reason="test", candidates=["person"]
        )
        assert result.data_class == "person"
        assert result.confidence == 0.9
        assert result.reason == "test"
        assert result.candidates == ["person"]


class TestClassifyEntryByTags:
    """Tests for classify_entry using tag-based heuristics."""

    def test_classify_person_by_tags(self) -> None:
        result = classify_entry(
            content="Daniel is the owner of Hive Mind",
            tags="durable,person",
        )
        assert result.data_class == "person"
        assert result.confidence >= 0.7

    def test_classify_technical_config_by_tags(self) -> None:
        result = classify_entry(
            content="Server runs on port 8420",
            tags="reviewable,technical",
        )
        assert result.data_class == "technical-config"
        assert result.confidence >= 0.7

    def test_classify_session_log_by_tags(self) -> None:
        result = classify_entry(
            content="Session 48ec54d4 was recovered manually.",
            tags="session",
        )
        assert result.data_class == "session-log"
        assert result.confidence >= 0.7

    def test_classify_epilogue_tagged_entries(self) -> None:
        result = classify_entry(
            content="Discussion about dark mode preferences",
            tags="session,epilogue",
        )
        assert result.data_class == "session-log"
        assert result.confidence >= 0.7


class TestClassifyEntryByContent:
    """Tests for classify_entry using content keyword heuristics."""

    def test_classify_preference_by_content(self) -> None:
        result = classify_entry(
            content="Daniel prefers dark mode and likes using Vim",
            tags="",
        )
        assert result.data_class == "preference"
        assert result.confidence >= 0.7

    def test_classify_timed_event_by_content(self) -> None:
        result = classify_entry(
            content="Meeting scheduled for 2026-03-10 at 3:00 PM",
            tags="",
        )
        assert result.data_class == "timed-event"
        assert result.confidence >= 0.7

    def test_classify_world_event_by_content(self) -> None:
        result = classify_entry(
            content="A mass shooting occurred on Sixth Street in Austin on March 1, 2026",
            tags="",
        )
        assert result.data_class == "world-event"
        assert result.confidence >= 0.7

    def test_classify_intention_by_content(self) -> None:
        result = classify_entry(
            content="Daniel plans to learn Japanese this year. His goal is fluency.",
            tags="",
        )
        assert result.data_class == "intention"
        assert result.confidence >= 0.7


class TestClassifyEntryEdgeCases:
    """Tests for edge cases and ambiguous content."""

    def test_classify_ambiguous_returns_low_confidence(self) -> None:
        result = classify_entry(
            content="Something happened",
            tags="",
        )
        assert result.confidence < 0.7

    def test_classify_returns_candidates_for_ambiguous(self) -> None:
        result = classify_entry(
            content="Something happened recently in the news",
            tags="",
        )
        assert isinstance(result.candidates, list)
        assert len(result.candidates) >= 1

    def test_classify_empty_content_returns_low_confidence(self) -> None:
        result = classify_entry(
            content="",
            tags="",
        )
        assert result.confidence < 0.7

    def test_classify_whitespace_content_returns_low_confidence(self) -> None:
        result = classify_entry(
            content="   ",
            tags="",
        )
        assert result.confidence < 0.7


class TestClassifyEntityNode:
    """Tests for classify_entity_node using entity type mappings."""

    def test_classify_entity_node_person_type(self) -> None:
        result = classify_entity_node(
            name="Daniel",
            entity_type="Person",
            properties={},
        )
        assert result.data_class == "person"
        assert result.confidence >= 0.7

    def test_classify_entity_node_preference_type(self) -> None:
        result = classify_entity_node(
            name="Dark Mode",
            entity_type="Preference",
            properties={},
        )
        assert result.data_class == "preference"
        assert result.confidence >= 0.7

    def test_classify_entity_node_project_type(self) -> None:
        result = classify_entity_node(
            name="Hive Mind",
            entity_type="Project",
            properties={},
        )
        assert result.data_class == "technical-config"
        assert result.confidence >= 0.7

    def test_classify_entity_node_system_type(self) -> None:
        result = classify_entity_node(
            name="Neo4j",
            entity_type="System",
            properties={},
        )
        assert result.data_class == "technical-config"
        assert result.confidence >= 0.7

    def test_classify_entity_node_concept_type(self) -> None:
        result = classify_entity_node(
            name="Machine Learning",
            entity_type="Concept",
            properties={},
        )
        # Concept is a catch-all; defaults to session-log with moderate confidence
        assert result.data_class is not None
        assert result.confidence >= 0.5
