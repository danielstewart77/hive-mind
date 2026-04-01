"""Test that docker-compose.yml has log rotation configured for the server service."""

from pathlib import Path

import yaml


class TestDockerLogRotation:
    """Verify docker-compose.yml has json-file log rotation on the server service."""

    def test_docker_compose_server_has_log_rotation(self):
        """Server service must have json-file logging with max-size=20m and max-file=5."""
        compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
        assert compose_path.exists(), f"docker-compose.yml not found at {compose_path}"

        with open(compose_path) as f:
            data = yaml.safe_load(f)

        server = data["services"]["server"]
        assert "logging" in server, "server service missing 'logging' key"

        logging_config = server["logging"]
        assert logging_config["driver"] == "json-file", (
            f"Expected driver 'json-file', got '{logging_config.get('driver')}'"
        )

        options = logging_config.get("options", {})
        assert options.get("max-size") == "20m", (
            f"Expected max-size '20m', got '{options.get('max-size')}'"
        )
        assert options.get("max-file") == "5", (
            f"Expected max-file '5', got '{options.get('max-file')}'"
        )
