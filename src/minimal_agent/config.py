"""Application configuration loaded from environment variables."""

import sysconfig
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _bundled_asset_path(relative_path: str) -> Path:
    """Resolve assets in a source checkout or an installed wheel."""

    source_path = Path(__file__).resolve().parents[2] / relative_path
    if source_path.is_file():
        return source_path
    return Path(sysconfig.get_path("data")) / "share" / "minimal-agent" / relative_path


class Settings(BaseSettings):
    """Validated process configuration.

    LLM credentials remain optional at application startup so health checks and Fake-LLM
    tests do not require external secrets. The real SDK client validates them when created.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    app_env: str = "local"
    database_path: Path = Path("var/minimal_agent.db")
    mainboard_data_path: Path = Field(
        default_factory=lambda: _bundled_asset_path("data/mainboards.json")
    )
    system_prompt_path: Path = Field(
        default_factory=lambda: _bundled_asset_path("prompts/system.md")
    )
    summarize_prompt_path: Path = Field(
        default_factory=lambda: _bundled_asset_path("prompts/summarize.md")
    )

    llm_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"),
    )
    llm_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_MODEL", "DEEPSEEK_MODEL", "OPENAI_MODEL"),
    )
    llm_base_url: HttpUrl | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_BASE_URL", "DEEPSEEK_BASE_URL", "OPENAI_BASE_URL"),
    )
    llm_api_style: Literal["responses", "chat_completions"] = "responses"
    llm_reasoning_capture: Literal["auto", "structured", "tagged", "both"] = "auto"
    llm_reasoning_output: Literal["auto", "raw", "summary", "off"] = "auto"
    llm_reasoning_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None
    ) = None
    llm_reasoning_summary: Literal["auto", "concise", "detailed"] | None = None
    llm_think_tag_enabled: bool = True
    llm_think_tag_name: str = Field(default="think", pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
    llm_max_output_tokens: int = Field(default=512, ge=1)

    agent_max_steps: int = Field(default=8, ge=1, le=32)
    agent_run_timeout_seconds: float = Field(default=60.0, gt=0)
    agent_shutdown_grace_seconds: float = Field(default=5.0, ge=0)
    user_message_max_chars: int = Field(default=20_000, ge=1, le=1_000_000)
    llm_timeout_seconds: float = Field(default=20.0, gt=0)
    llm_max_retries: int = Field(default=3, ge=0, le=10)
    tool_timeout_seconds: float = Field(default=10.0, gt=0)
    weather_http_timeout_seconds: float = Field(default=5.0, gt=0)

    context_window_tokens: int = Field(default=0, ge=0)
    context_trigger_ratio: float = Field(default=0.80, gt=0, lt=1)
    recent_turns_to_keep: int = Field(default=5, ge=1)

    stream_flush_bytes: int = Field(default=8192, ge=1)
    stream_flush_interval_ms: int = Field(default=100, ge=1)
    stream_queue_maxsize: int = Field(default=256, ge=1)
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide immutable settings instance."""

    return Settings()
