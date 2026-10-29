"""Application settings loaded from environment variables / .env file."""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the Lichess club outreach tool."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    lichess_token: str = ""
    lichess_username: str = ""
    senpai_team_id: str = ""
    source_club_id: str = ""

    llm_api_key: str = ""
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "openai/gpt-4o-mini"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 400
    llm_timeout: int = 60
    message_language: str = "ru"
    club_url: str = ""

    database_url: str = "sqlite:///./data/outreach.db"

    send_interval_seconds: int = 45
    daily_send_limit: int = 30
    # Safety pause after a 429. This is our own conservative value, not an
    # official Lichess limit (the platform does not publish exact numbers).
    rate_limit_cooldown_seconds: int = 900
    # A ``sending`` row older than this is considered abandoned and is retried.
    stale_sending_seconds: int = 600
    min_rapid: int = 0
    max_rapid: int = 9999

    admin_token: str = ""

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        """Heroku ships ``postgres://`` URLs, SQLAlchemy 2.x requires ``postgresql://``."""
        if value.startswith("postgres://"):
            return "postgresql://" + value[len("postgres://") :]
        return value


settings = Settings()
