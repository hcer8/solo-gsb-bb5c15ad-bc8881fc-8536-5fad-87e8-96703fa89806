"""Service configuration, driven by COSTGUARD_* environment variables."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="COSTGUARD_",
        env_file=".env",
        extra="ignore",
    )

    db_path: str = "data/costguard.db"
    default_list_multiplier: int = Field(default=10, ge=1)
    max_depth: int = Field(default=50, ge=1)
    max_expanded_nodes: int = Field(default=20_000, ge=100)
    cost_model_version: str = "v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
