from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AnkiSettings(BaseSettings):
    """Settings needed to open and sync the collection (bootstrap uses only these)."""

    model_config = SettingsConfigDict(extra="ignore")

    anki_user: str
    anki_password: SecretStr
    # Required: an empty endpoint would make pylib fall back to its built-in default server.
    anki_sync_endpoint: str
    anki_deck: str = "Japanese::Bot"
    anki_notetype: str = "JP Bot Vocab"
    dedup_extra_sources: str = ""
    data_dir: Path = Path("/data")
    log_level: str = "INFO"

    @field_validator("anki_sync_endpoint")
    @classmethod
    def _require_endpoint(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("ANKI_SYNC_ENDPOINT must point to a self-hosted Anki sync server")
        return value

    @property
    def collection_path(self) -> Path:
        return self.data_dir / "collection.anki2"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / ".lock"

    @property
    def full_sync_marker_path(self) -> Path:
        return self.data_dir / "full_sync_required"


class Settings(AnkiSettings):
    tg_token: SecretStr
    tg_allowed_user_id: int
    pending_ttl_hours: float = 24
    sync_debounce_seconds: float = 7200
    sync_stale_hours: float = 72
    pre_sync_timeout_seconds: float = 15
    shutdown_sync_timeout_seconds: float = 45
    # Audio is off by default: "edge" uses edge-tts, an unofficial Microsoft Edge endpoint.
    tts_provider: Literal["none", "edge"] = "none"
    tts_voice: str = "ja-JP-NanamiNeural"
    tts_timeout_seconds: float = 15
    deepl_api_key: SecretStr | None = None
    deepl_timeout_seconds: float = 10
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_timeout_seconds: float = 10
    # DeepL target_lang code (e.g. ZH-HANT, KO, EN-US); also the cache key.
    translation_target: str = "EN-US"
    # Language name for the Gemini prompt; empty uses a built-in name or the code itself.
    translation_target_name: str = ""
    include_proper_nouns: bool = False
    # Built-in UI language: a file name in src/ankibot/locales/ (en, zh-Hant).
    bot_language: str = "en"
    # Optional TOML file whose keys replace the built-in UI text (any subset, same format as the locales).
    bot_messages_file: Path | None = None
    # Touched after every successful Telegram getMe; read by `python -m ankibot.healthcheck`.
    heartbeat_file: Path = Path("/tmp/ankibot-heartbeat")
