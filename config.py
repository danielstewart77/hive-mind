"""
Hive Mind — Centralized configuration.

Reads NON-SECRET settings from config.yaml.
Secrets: keyring first (via 'hive-mind' service), env fallback.
"""

import os

import yaml
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _get_secret(key: str, default: str = "") -> str:
    """Get a secret — keyring first, env fallback.

    Inline to avoid circular import with agents/secret_manager.py.
    """
    try:
        import keyring
        val = keyring.get_password("hive-mind", key)
        if val:
            return val
    except Exception:
        pass
    return os.environ.get(key, default)

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
class ScheduledTask:
    cron: str
    prompt: str
    voice: bool = True
    notify: bool = True  # False = run for side effects only, no Telegram delivery
    timezone: str = "America/Chicago"


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

    # Group chat configuration
    group_chat: dict = field(default_factory=dict)

    # MCP server
    mcp_port: int = 7777

    # Discord bot
    discord_allowed_users: list[int] = field(default_factory=list)
    discord_allowed_channels: list[int] = field(default_factory=list)

    # Telegram bot
    telegram_allowed_users: list[int] = field(default_factory=list)
    telegram_owner_chat_id: int = 0  # DM chat ID for HITL approval notifications

    # HITL (Human-in-the-Loop)
    hitl_internal_token: str = ""  # shared secret between gateway and bot

    # Scheduled tasks
    scheduled_tasks: list[ScheduledTask] = field(default_factory=list)

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
            group_chat=_yaml_config.get("group_chat", {}),
            mcp_port=_yaml_config.get("mcp_port", 7777),
            discord_allowed_users=_yaml_config.get("discord_allowed_users", []),
            discord_allowed_channels=_yaml_config.get("discord_allowed_channels", []),
            telegram_allowed_users=_yaml_config.get("telegram_allowed_users", []),
            telegram_owner_chat_id=_yaml_config.get("telegram_owner_chat_id", 0),
            hitl_internal_token=_get_secret("HITL_INTERNAL_TOKEN"),
            scheduled_tasks=[
                ScheduledTask(
                    cron=t["cron"],
                    prompt=t["prompt"],
                    voice=t.get("voice", True),
                    notify=t.get("notify", True),
                    timezone=t.get("timezone", "America/Chicago"),
                )
                for t in _yaml_config.get("scheduled_tasks", [])
            ],
        )


config = HiveMindConfig.from_yaml()
