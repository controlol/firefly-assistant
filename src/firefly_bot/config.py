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
        "application/xml",
        "text/xml",
    )
    # Once an invoice is attached, the email is MOVED to this folder (created if missing) so the
    # INBOX shows only pending/problem mail. A move, not a delete — the email is kept.
    processed_folder: str = "Processed"


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


class EnrichSettings(BaseSettings):
    """Local embedding enricher (Phase 2). See docs/ENRICHMENT.md + docs/COLD_START.md."""

    model_config = SettingsConfigDict(
        env_prefix="ENRICH_", env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    enabled: bool = True
    model_name: str = "intfloat/multilingual-e5-small"
    # Absolute top-similarity gate (NOT margin): e5 gives high baseline similarities with small
    # inter-category margins, so we threshold the absolute cosine. Below it -> needs-review.
    gate: float = 0.83
    # k-NN over a tiny example set yields spurious *near-floor* matches that can outrank the
    # correct, curated label name. So k-NN only overrules a gated zero-shot label-name match when
    # it is clearly confident (>= this); below it, the curated label name is trusted first.
    knn_trust: float = 0.90
    # New-label discovery (Phase 2.2): cosine link threshold for clustering orphan transactions,
    # and the minimum cluster size before a missing category is proposed. 0.90 because e5's high
    # similarity floor only separates distinct themes around there (single-link chains below it).
    discover_threshold: float = 0.90
    discover_min_size: int = 3
    # Merchant entity resolution (Phase 2.3): use embeddings as the LAST resort in AccountResolver,
    # AFTER IBAN-exact, normalise_merchant-exact, and rapidfuzz all miss, to collapse spelling
    # variants of one merchant ("Albert Heijn" / "AH to go" / "Appie") onto one opposing account.
    #
    # OFF BY DEFAULT and deliberately conservative: wrongly MERGING two *distinct* merchants into
    # one account corrupts the ledger and is far worse than harmless account proliferation. e5 has a
    # high similarity floor (~0.84) with small inter-class margins, so naive embedding matching will
    # false-merge. The probe (experiments/merchant_resolution_probe.py) showed distinct Dutch
    # merchants also score high, so this stays opt-in behind a HIGH gate; only enable it if the
    # probe numbers on your own data clear the gate with real margin.
    merchant_resolution: bool = False
    # Minimum cosine for the embedding step to reuse an existing account. High on purpose: a
    # false MERGE is unrecoverable, a false MISS just makes a duplicate account.
    merchant_gate: float = 0.93


class Settings(BaseSettings):
    """Top-level settings aggregating the sub-configs."""

    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    imap: ImapSettings = Field(default_factory=ImapSettings)
    firefly: FireflySettings = Field(default_factory=FireflySettings)
    matching: MatchingSettings = Field(default_factory=MatchingSettings)
    bank: BankSettings = Field(default_factory=BankSettings)
    enrich: EnrichSettings = Field(default_factory=EnrichSettings)

    report_dir: str = "./reports"
    # Append-only training data (Phase 1 label capture) lives under here, gitignored.
    data_dir: str = "./data"
    # Rasterisation DPI for PDFs before OCR. 200 is a good accuracy/speed balance for invoices.
    ocr_dpi: int = 200
    # Only relevant to the optional PaddleOCR engine; RapidOCR (default) is CPU-only.
    ocr_use_gpu: bool = False

    @property
    def labels_path(self) -> str:
        """Path to the append-only JSONL of captured decisions."""
        return f"{self.data_dir}/labels.jsonl"


def load_settings() -> Settings:
    return Settings()
