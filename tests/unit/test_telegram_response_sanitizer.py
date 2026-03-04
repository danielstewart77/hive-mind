"""Unit tests for _looks_like_json and _sanitize_response helpers in telegram_bot."""


class TestLooksLikeJson:
    """Tests for the _looks_like_json helper function."""

    def test_looks_like_json_detects_json_object(self) -> None:
        from clients.telegram_bot import _looks_like_json

        assert _looks_like_json('{"status": "completed"}') is True

    def test_looks_like_json_detects_json_array(self) -> None:
        from clients.telegram_bot import _looks_like_json

        assert _looks_like_json('[{"id": "abc"}]') is True

    def test_looks_like_json_rejects_plain_text(self) -> None:
        from clients.telegram_bot import _looks_like_json

        assert _looks_like_json("Hello world") is False

    def test_looks_like_json_rejects_text_with_braces(self) -> None:
        from clients.telegram_bot import _looks_like_json

        assert _looks_like_json("Use {name} as a placeholder") is False

    def test_looks_like_json_handles_empty_string(self) -> None:
        from clients.telegram_bot import _looks_like_json

        assert _looks_like_json("") is False

    def test_looks_like_json_handles_whitespace_wrapped_json(self) -> None:
        from clients.telegram_bot import _looks_like_json

        assert _looks_like_json('  {"key": "val"}  ') is True

    def test_looks_like_json_rejects_number(self) -> None:
        from clients.telegram_bot import _looks_like_json

        assert _looks_like_json("42") is False

    def test_looks_like_json_rejects_json_string(self) -> None:
        from clients.telegram_bot import _looks_like_json

        assert _looks_like_json('"just a string"') is False


class TestSanitizeResponse:
    """Tests for the _sanitize_response helper function."""

    def test_sanitize_response_passes_plain_text(self) -> None:
        from clients.telegram_bot import _sanitize_response

        assert _sanitize_response("Hello there") == "Hello there"

    def test_sanitize_response_replaces_json_with_confirmation(self) -> None:
        from clients.telegram_bot import _sanitize_response

        result = _sanitize_response('{"status": "completed", "session_id": "abc"}')
        assert "{" not in result
        assert result == "Done."

    def test_sanitize_response_preserves_no_response(self) -> None:
        from clients.telegram_bot import _sanitize_response

        assert _sanitize_response("(No response)") == "(No response)"

    def test_sanitize_response_replaces_json_array(self) -> None:
        from clients.telegram_bot import _sanitize_response

        result = _sanitize_response('[{"id": "abc"}]')
        assert "[" not in result
        assert result == "Done."

    def test_sanitize_response_preserves_text_with_braces(self) -> None:
        from clients.telegram_bot import _sanitize_response

        text = "Use {name} as a placeholder"
        assert _sanitize_response(text) == text
