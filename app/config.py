"""Application settings loaded from env."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "local"
    app_debug: bool = True
    log_level: str = "INFO"

    database_url: str = Field(
        default="postgresql+asyncpg://pandora:pandora@localhost:5432/pandora_conversion"
    )

    # Pandora Core Identity
    pandora_core_base_url: str = "http://localhost:8001"
    pandora_core_issuer: str = "https://id.js-store.com.tw"
    pandora_core_public_key_ttl: int = 3600  # seconds
    pandora_core_allowed_products: str = (
        "pandora_js_store,doudou,fairy_calendar,fairy_skin,fairy_academy"
    )

    internal_shared_secret: str = "change-me"

    @property
    def allowed_products(self) -> set[str]:
        return {p.strip() for p in self.pandora_core_allowed_products.split(",") if p.strip()}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
