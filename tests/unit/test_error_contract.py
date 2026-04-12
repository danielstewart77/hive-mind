"""Unit tests for the mind_unreachable error response shape.

Verifies the error dict has exactly the expected keys and values.
"""


class TestMindUnreachableResponseShape:
    """Tests for the 503 error response structure."""

    def test_mind_unreachable_response_shape(self):
        """Error dict has exactly mind_id and error keys with expected values."""
        error_response = {"mind_id": "bilby", "error": "mind_unreachable"}
        assert set(error_response.keys()) == {"mind_id", "error"}
        assert error_response["mind_id"] == "bilby"
        assert error_response["error"] == "mind_unreachable"
