"""Typed configuration loaded from environment / .env (see .env.example)."""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Shared: read from process env and a local .env, ignore unrelated keys.
_ENV_FILE = ".env"


class ImapSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="IMAP_", env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

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
    # Custom IMAP keyword set on a message once its invoice has been attached. Messages with this
    # keyword are skipped on the next run; emails are never deleted.
    processed_keyword: str = "FireflyProcessed"


class FireflySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FIREFLY_", env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    base_url: str = Field(description="e.g. https://firefly.example.com")
    token: SecretStr = Field(description="Personal Access Token.")
    verify_tls: bool = True


class MatchingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MATCH_", env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    # A payment usually posts a few days to a few weeks after the invoice date.
    date_window_days: int = 30
    # Absolute amount tolerance (currency units) when comparing invoice total to txn amount.
    amount_tolerance: Decimal = Decimal("0.02")
    # Minimum score to auto-attach without the needs-review tag.
    auto_attach_threshold: float = 0.8
    needs_review_tag: str = "needs-review"
    processed_tag: str = "firefly-bot"


class BankSettings(BaseSettings):
    """Statement-import settings (CAMT.053)."""

    model_config = SettingsConfigDict(
        env_prefix="BANK_", env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    # Account holder name; counterparties with this name are treated as your own accounts
    # (savings) and imported as transfers instead of income/expense.
    owner_name: str | None = None
    # Extra own-account IBANs (e.g. savings) if name detection is not enough.
    own_ibans: tuple[str, ...] = ()
    account_name: str = "Betaalrekening"


class Settings(BaseSettings):
    """Top-level settings aggregating the sub-configs."""

    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    imap: ImapSettings = Field(default_factory=ImapSettings)
    firefly: FireflySettings = Field(default_factory=FireflySettings)
    matching: MatchingSettings = Field(default_factory=MatchingSettings)
    bank: BankSettings = Field(default_factory=BankSettings)

    report_dir: str = "./reports"
    # Rasterisation DPI for PDFs before OCR. 200 is a good accuracy/speed balance for invoices.
    ocr_dpi: int = 200
    # Only relevant to the optional PaddleOCR engine; RapidOCR (default) is CPU-only.
    ocr_use_gpu: bool = False


def load_settings() -> Settings:
    return Settings()
