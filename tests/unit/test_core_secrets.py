"""Unit tests for core.secrets shared credential utility."""

from unittest.mock import patch



class TestGetCredential:
    """Tests for get_credential() function."""

    @patch("core.secrets.keyring")
    def test_get_credential_returns_keyring_value(self, mock_keyring):
        """Asserts keyring value returned when available."""
        mock_keyring.get_password.return_value = "my-secret-value"

        from core.secrets import get_credential

        result = get_credential("MY_API_KEY")
        assert result == "my-secret-value"
        mock_keyring.get_password.assert_called_once_with("hive-mind", "MY_API_KEY")

    @patch("core.secrets.keyring")
    def test_get_credential_falls_back_to_env(self, mock_keyring, monkeypatch):
        """Asserts env var returned when keyring returns None."""
        mock_keyring.get_password.return_value = None
        monkeypatch.setenv("MY_API_KEY", "env-secret-value")

        from core.secrets import get_credential

        result = get_credential("MY_API_KEY")
        assert result == "env-secret-value"

    @patch("core.secrets.keyring")
    def test_get_credential_returns_none_when_both_missing(self, mock_keyring, monkeypatch):
        """Asserts None when neither source has the key."""
        mock_keyring.get_password.return_value = None
        monkeypatch.delenv("NONEXISTENT_KEY", raising=False)

        from core.secrets import get_credential

        result = get_credential("NONEXISTENT_KEY")
        assert result is None

    @patch("core.secrets.keyring")
    def test_get_credential_keyring_exception_falls_back(self, mock_keyring, monkeypatch):
        """Asserts env fallback when keyring raises."""
        mock_keyring.get_password.side_effect = Exception("keyring error")
        monkeypatch.setenv("MY_API_KEY", "fallback-value")

        from core.secrets import get_credential

        result = get_credential("MY_API_KEY")
        assert result == "fallback-value"
