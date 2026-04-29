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

    # Mothership (pandora-js-store) — ADR-003 loyalist rule needs 母艦
    # order summary. When base_url+secret are unset we fall back to the
    # stub client (loyalist's repeat-purchase branch silently no-ops, by
    # design — see ADR-003 §6 「保守誤殺」).
    mothership_base_url: str = ""
    mothership_internal_secret: str = ""
    mothership_timeout: float = 5.0

    # Lifecycle cache-invalidate fan-out (PG-93)
    # Consumer registry via env: LIFECYCLE_INVALIDATE_CONSUMERS=pandora_meal,...
    # Per consumer: LIFECYCLE_INVALIDATE_CONSUMER_<NAME>_URL / _SECRET
    # Empty = no fan-out (transitions still persist; consumers fall back to TTL).
    lifecycle_invalidate_timeout: float = 3.0

    # Gamification webhook fan-out (ADR-009 §2.2)
    # Comma-separated list of consumer names. For each, expect:
    #   GAMIFICATION_CONSUMER_<NAME>_URL
    #   GAMIFICATION_CONSUMER_<NAME>_SECRET
    # Empty = no fan-out (events still write to outbox; dispatcher idle).
    gamification_consumers: str = ""
    gamification_dispatch_timeout: float = 5.0
    # Replay-protection window for receivers (informational; we set the
    # X-Pandora-Timestamp header at send time and consumers should reject
    # events older than this window).
    gamification_max_clock_skew_seconds: int = 300

    @property
    def gamification_consumer_names(self) -> list[str]:
        return [c.strip() for c in self.gamification_consumers.split(",") if c.strip()]

    @property
    def allowed_products(self) -> set[str]:
        return {p.strip() for p in self.pandora_core_allowed_products.split(",") if p.strip()}

    @property
    def mothership_http_enabled(self) -> bool:
        """True iff both base_url and secret are configured."""
        return bool(self.mothership_base_url) and bool(self.mothership_internal_secret)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
