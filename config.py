"""
Hive Mind — Centralized configuration.

Reads NON-SECRET settings from config.yaml and SECRETS from .env.
NEVER put config in .env - that file is for secrets ONLY.
"""

import yaml
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_DIR = Path(__file__).parent.resolve()

# Load config.yaml (non-secrets)
CONFIG_FILE = PROJECT_DIR / "config.yaml"
_yaml_config = {}
if CONFIG_FILE.exists():
    with open(CONFIG_FILE) as f:
        _yaml_config = yaml.safe_load(f) or {}


@dataclass
class AutopilotGuards:
    max_budget_usd: float = 5.00
    max_turns_without_input: int = 50
    max_minutes_without_input: int = 30


@dataclass
class HiveMindConfig:
    # Gateway server
    server_port: int = 8420
    idle_timeout_minutes: int = 30
    max_sessions: int = 10
    default_model: str = "sonnet"

    # Autopilot guard rails
    autopilot_guards: AutopilotGuards = field(default_factory=AutopilotGuards)

    # Provider configs: {name: {env: {...}, api_base: "..."}}
    providers: dict = field(default_factory=dict)

    # Static model -> provider mappings
    models: dict[str, str] = field(default_factory=dict)

    # MCP server
    mcp_port: int = 7777

    # Discord bot
    discord_allowed_users: list[int] = field(default_factory=list)
    discord_allowed_channels: list[int] = field(default_factory=list)

    # Telegram bot
    telegram_allowed_users: list[int] = field(default_factory=list)

    @classmethod
    def from_yaml(cls) -> "HiveMindConfig":
        """Load config from config.yaml."""
        guards_raw = _yaml_config.get("autopilot_guards", {})
        guards = AutopilotGuards(
            max_budget_usd=guards_raw.get("max_budget_usd", 5.00),
            max_turns_without_input=guards_raw.get("max_turns_without_input", 50),
            max_minutes_without_input=guards_raw.get("max_minutes_without_input", 30),
        )

        return cls(
            server_port=_yaml_config.get("server_port", 8420),
            idle_timeout_minutes=_yaml_config.get("idle_timeout_minutes", 30),
            max_sessions=_yaml_config.get("max_sessions", 10),
            default_model=_yaml_config.get("default_model", "sonnet"),
            autopilot_guards=guards,
            providers=_yaml_config.get("providers", {}),
            models=_yaml_config.get("models", {}),
            mcp_port=_yaml_config.get("mcp_port", 7777),
            discord_allowed_users=_yaml_config.get("discord_allowed_users", []),
            discord_allowed_channels=_yaml_config.get("discord_allowed_channels", []),
            telegram_allowed_users=_yaml_config.get("telegram_allowed_users", []),
        )


config = HiveMindConfig.from_yaml()
