"""Typed configuration loaded from environment / .env (see .env.example)."""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ImapSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IMAP_")

    host: str
    port: int = 993
    username: str
    password: SecretStr
    mailbox: str = "INBOX"
    # Only treat these attachment content-types as candidate documents.
    allowed_content_types: tuple[str, ...] = (
        "application/pdf",
        "image/png",
        "image/jpeg",
    )
    mark_seen: bool = True


class FireflySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FIREFLY_")

    base_url: str = Field(description="e.g. https://firefly.example.com")
    token: SecretStr = Field(description="Personal Access Token.")
    verify_tls: bool = True


class MatchingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MATCH_")

    # A payment usually posts a few days to a few weeks after the invoice date.
    date_window_days: int = 30
    # Absolute amount tolerance (currency units) when comparing invoice total to txn amount.
    amount_tolerance: Decimal = Decimal("0.02")
    # Minimum score to auto-attach without the needs-review tag.
    auto_attach_threshold: float = 0.8
    needs_review_tag: str = "needs-review"
    processed_tag: str = "firefly-bot"


class Settings(BaseSettings):
    """Top-level settings aggregating the sub-configs."""

    imap: ImapSettings = Field(default_factory=ImapSettings)  # type: ignore[arg-type]
    firefly: FireflySettings = Field(default_factory=FireflySettings)  # type: ignore[arg-type]
    matching: MatchingSettings = Field(default_factory=MatchingSettings)

    report_dir: str = "./reports"
    # Rasterisation DPI for PDFs before OCR. 200 is a good accuracy/speed balance for invoices.
    ocr_dpi: int = 200
    # Only relevant to the optional PaddleOCR engine; RapidOCR (default) is CPU-only.
    ocr_use_gpu: bool = False


def load_settings() -> Settings:
    return Settings()
