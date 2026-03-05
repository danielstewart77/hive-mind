"""Unit tests for memory expiry: recurring detection, expires_at validation, and build_metadata integration."""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_neo4j_and_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure agents.memory can be imported by mocking neo4j and keyring."""
    if "neo4j" not in sys.modules:
        neo4j_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)
    if "agent_tooling" not in sys.modules:
        at_mock = MagicMock()
        at_mock.tool = MagicMock(return_value=lambda f: f)
        monkeypatch.setitem(sys.modules, "agent_tooling", at_mock)


# ---------------------------------------------------------------------------
# Step 1: detect_recurring tests
# ---------------------------------------------------------------------------
class TestDetectRecurring:
    """Tests for the detect_recurring function in core.memory_schema."""

    def test_detect_recurring_birthday_returns_true(self) -> None:
        from core.memory_schema import detect_recurring
        assert detect_recurring("Mom's birthday party") is True

    def test_detect_recurring_anniversary_returns_true(self) -> None:
        from core.memory_schema import detect_recurring
        assert detect_recurring("Wedding anniversary dinner") is True

    def test_detect_recurring_weekly_returns_true(self) -> None:
        from core.memory_schema import detect_recurring
        assert detect_recurring("Weekly standup meeting") is True

    def test_detect_recurring_monthly_returns_true(self) -> None:
        from core.memory_schema import detect_recurring
        assert detect_recurring("Monthly review") is True

    def test_detect_recurring_annual_returns_true(self) -> None:
        from core.memory_schema import detect_recurring
        assert detect_recurring("Annual performance review") is True

    def test_detect_recurring_every_returns_true(self) -> None:
        from core.memory_schema import detect_recurring
        assert detect_recurring("Every Tuesday yoga class") is True

    def test_detect_recurring_keyword_recurring_returns_true(self) -> None:
        from core.memory_schema import detect_recurring
        assert detect_recurring("Recurring team sync") is True

    def test_detect_recurring_no_keywords_returns_false(self) -> None:
        from core.memory_schema import detect_recurring
        assert detect_recurring("Doctor appointment tomorrow") is False

    def test_detect_recurring_case_insensitive(self) -> None:
        from core.memory_schema import detect_recurring
        assert detect_recurring("BIRTHDAY celebration") is True

    def test_detect_recurring_partial_word_annually_no_match(self) -> None:
        from core.memory_schema import detect_recurring
        # "annually" should NOT match "annual" with trailing word boundary
        assert detect_recurring("I am annually reviewing") is False

    def test_detect_recurring_word_annual_matches(self) -> None:
        from core.memory_schema import detect_recurring
        # "annual" as a standalone word should still match
        assert detect_recurring("annual review meeting") is True

    def test_detect_recurring_everyday_no_match(self) -> None:
        from core.memory_schema import detect_recurring
        # "everyday" should NOT match "every" -- trailing \b prevents it
        assert detect_recurring("This is an everyday task") is False

    def test_detect_recurring_everything_no_match(self) -> None:
        from core.memory_schema import detect_recurring
        # "everything" should NOT match "every" -- trailing \b prevents it
        assert detect_recurring("everything is fine") is False

    def test_detect_recurring_every_standalone_matches(self) -> None:
        from core.memory_schema import detect_recurring
        # "every" as standalone word should still match
        assert detect_recurring("every morning jog") is True

    def test_detect_recurring_empty_string(self) -> None:
        from core.memory_schema import detect_recurring
        assert detect_recurring("") is False


# ---------------------------------------------------------------------------
# Step 2: validate_expires_at tests
# ---------------------------------------------------------------------------
class TestValidateExpiresAt:
    """Tests for the validate_expires_at function in core.memory_schema."""

    def test_validate_expires_at_valid_iso_returns_string(self) -> None:
        from core.memory_schema import validate_expires_at
        result = validate_expires_at("2026-04-01T15:00:00Z")
        assert isinstance(result, str)

    def test_validate_expires_at_with_timezone_offset(self) -> None:
        from core.memory_schema import validate_expires_at
        result = validate_expires_at("2026-04-01T15:00:00-05:00")
        assert isinstance(result, str)

    def test_validate_expires_at_no_timezone_returns_string(self) -> None:
        from core.memory_schema import validate_expires_at
        result = validate_expires_at("2026-04-01T15:00:00")
        assert isinstance(result, str)

    def test_validate_expires_at_invalid_format_raises(self) -> None:
        from core.memory_schema import validate_expires_at
        with pytest.raises(ValueError, match="resolved absolute ISO datetime"):
            validate_expires_at("next Tuesday at 3pm")

    def test_validate_expires_at_relative_time_raises(self) -> None:
        from core.memory_schema import validate_expires_at
        with pytest.raises(ValueError):
            validate_expires_at("tomorrow")

    def test_validate_expires_at_empty_string_raises(self) -> None:
        from core.memory_schema import validate_expires_at
        with pytest.raises(ValueError):
            validate_expires_at("")

    def test_validate_expires_at_date_only_raises(self) -> None:
        from core.memory_schema import validate_expires_at
        with pytest.raises(ValueError):
            validate_expires_at("2026-04-01")


# ---------------------------------------------------------------------------
# Step 3: build_metadata integration with recurring and expires_at validation
# ---------------------------------------------------------------------------
class TestBuildMetadataTimedEventExpiry:
    """Tests for build_metadata with timed-event recurring/expires_at integration."""

    def test_build_metadata_timed_event_validates_expires_format(self) -> None:
        from core.memory_schema import build_metadata
        with pytest.raises(ValueError):
            build_metadata(
                data_class="timed-event",
                source="user",
                expires_at="next Tuesday",
            )

    def test_build_metadata_timed_event_valid_expires_passes(self) -> None:
        from core.memory_schema import build_metadata
        result = build_metadata(
            data_class="timed-event",
            source="user",
            expires_at="2026-04-01T15:00:00Z",
        )
        assert "expires_at" in result
        assert "recurring" in result

    def test_build_metadata_timed_event_recurring_from_content(self) -> None:
        from core.memory_schema import build_metadata
        result = build_metadata(
            data_class="timed-event",
            source="user",
            expires_at="2026-04-01T15:00:00Z",
            content="Mom's birthday dinner",
        )
        assert result["recurring"] is True

    def test_build_metadata_timed_event_not_recurring_by_default(self) -> None:
        from core.memory_schema import build_metadata
        result = build_metadata(
            data_class="timed-event",
            source="user",
            expires_at="2026-04-01T15:00:00Z",
            content="Doctor appointment",
        )
        assert result["recurring"] is False

    def test_build_metadata_timed_event_explicit_recurring_override(self) -> None:
        from core.memory_schema import build_metadata
        result = build_metadata(
            data_class="timed-event",
            source="user",
            expires_at="2026-04-01T15:00:00Z",
            content="Doctor appointment",
            recurring=True,
        )
        assert result["recurring"] is True

    def test_build_metadata_non_timed_event_ignores_recurring(self) -> None:
        from core.memory_schema import build_metadata
        result = build_metadata(data_class="person", source="user")
        assert "recurring" not in result

    def test_build_metadata_timed_event_no_content_defaults_recurring_false(self) -> None:
        from core.memory_schema import build_metadata
        result = build_metadata(
            data_class="timed-event",
            source="user",
            expires_at="2026-04-01T15:00:00Z",
        )
        assert result["recurring"] is False

    def test_build_metadata_timed_event_no_content_explicit_recurring_true(self) -> None:
        from core.memory_schema import build_metadata
        result = build_metadata(
            data_class="timed-event",
            source="user",
            expires_at="2026-04-01T15:00:00Z",
            recurring=True,
        )
        assert result["recurring"] is True


# ---------------------------------------------------------------------------
# Step 4: memory_store / memory_store_direct with recurring parameter
# ---------------------------------------------------------------------------
def _make_mock_driver() -> MagicMock:
    """Create a mock Neo4j driver with session and run mocked."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    mock_result = MagicMock()
    mock_result.single.return_value = {"id": "test-id-123"}
    mock_session.run.return_value = mock_result
    return mock_driver


class TestMemoryStoreDirectRecurring:
    """Tests for memory_store_direct with recurring parameter."""

    def test_memory_store_direct_timed_event_with_recurring_stores_property(self) -> None:
        mock_driver = _make_mock_driver()
        import agents.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_store_direct(
                content="Mom's birthday dinner",
                data_class="timed-event",
                source="user",
                expires_at="2026-04-01T15:00:00Z",
            )
            result = json.loads(result_str)
            assert result["stored"] is True
            mock_session = mock_driver.session.return_value.__enter__.return_value
            call_args = mock_session.run.call_args
            params = call_args[1]
            assert params["recurring"] is True

    def test_memory_store_direct_timed_event_explicit_recurring_false(self) -> None:
        mock_driver = _make_mock_driver()
        import agents.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_store_direct(
                content="Mom's birthday dinner",
                data_class="timed-event",
                source="user",
                expires_at="2026-04-01T15:00:00Z",
                recurring=False,
            )
            result = json.loads(result_str)
            assert result["stored"] is True
            mock_session = mock_driver.session.return_value.__enter__.return_value
            call_args = mock_session.run.call_args
            params = call_args[1]
            assert params["recurring"] is False

    def test_memory_store_direct_timed_event_without_expires_returns_error(self) -> None:
        mock_driver = _make_mock_driver()
        import agents.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_store_direct(
                content="Meeting at 3pm",
                data_class="timed-event",
                source="user",
            )
            result = json.loads(result_str)
            assert result["stored"] is False

    def test_memory_store_direct_timed_event_invalid_expires_returns_error(self) -> None:
        mock_driver = _make_mock_driver()
        import agents.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_store_direct(
                content="Meeting next Friday",
                data_class="timed-event",
                source="user",
                expires_at="next Friday",
            )
            result = json.loads(result_str)
            assert result["stored"] is False

    def test_memory_store_direct_non_timed_event_no_recurring_property(self) -> None:
        mock_driver = _make_mock_driver()
        import agents.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_store_direct(
                content="Daniel is a person",
                data_class="person",
                source="user",
            )
            result = json.loads(result_str)
            assert result["stored"] is True
            mock_session = mock_driver.session.return_value.__enter__.return_value
            call_args = mock_session.run.call_args
            params = call_args[1]
            # For non-timed-event, recurring should not be present or should be None
            assert params.get("recurring") is None

    def test_memory_store_timed_event_hitl_approved_stores_recurring(self) -> None:
        mock_driver = _make_mock_driver()
        import agents.memory as mem_mod

        with (
            patch.object(mem_mod, "_hitl_gate", return_value=True),
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_store(
                content="Mom's birthday dinner",
                data_class="timed-event",
                source="user",
                expires_at="2026-04-01T15:00:00Z",
            )
            result = json.loads(result_str)
            assert result["stored"] is True
            mock_session = mock_driver.session.return_value.__enter__.return_value
            call_args = mock_session.run.call_args
            params = call_args[1]
            assert params["recurring"] is True
