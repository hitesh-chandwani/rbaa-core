"""Application settings, read from environment variables (and a local `.env` file).

Every field here must have a matching entry in `.env.example`; a test enforces it.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Read but not used or validated yet (see issue #11).
    google_api_key: str = ""
    database_url: str = "postgresql://rbaa:rbaa_local_dev@localhost:5432/rbaa"
    redis_url: str = "redis://localhost:6379/0"


def get_settings() -> Settings:
    return Settings()
