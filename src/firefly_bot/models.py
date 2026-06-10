"""Typed domain models shared across the pipeline.

Everything that crosses a module boundary is one of these pydantic models, so the whole
pipeline is statically checkable with `mypy --strict`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum, StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Attachment(BaseModel):
    """A raw file pulled from an email, before any OCR."""

    model_config = ConfigDict(frozen=True)

    filename: str
    content_type: str
    data: bytes
    sha256: str = Field(description="Content hash, used to dedup across runs.")
    source_message_id: str
    received_at: datetime
    source_uid: str | None = Field(
        default=None, description="IMAP UID of the message, so it can be marked processed."
    )


class FieldConfidence(float, Enum):
    """Coarse confidence buckets for an extracted field (kept simple for iteration 1)."""

    HIGH = 1.0
    MEDIUM = 0.6
    LOW = 0.3
    NONE = 0.0


class ExtractedInvoice(BaseModel):
    """Structured result of OCR + heuristics over a single document.

    Iteration 1 only requires `total_amount` and `counterparty_iban`; the rest are populated
    opportunistically and filled in by iteration 2 (business name, VAT rows, line items).
    """

    source: Attachment
    total_amount: Decimal | None = None
    currency: str = "EUR"
    counterparty_iban: str | None = None
    counterparty_name: str | None = None
    invoice_date: date | None = None
    invoice_number: str | None = None
    raw_text: str = Field(default="", repr=False)

    # Per-field confidence so the matcher and report can reason about reliability.
    total_confidence: FieldConfidence = FieldConfidence.NONE
    iban_confidence: FieldConfidence = FieldConfidence.NONE
    number_confidence: FieldConfidence = FieldConfidence.NONE
    date_confidence: FieldConfidence = FieldConfidence.NONE

    @property
    def is_actionable(self) -> bool:
        """True when we have at least one identifier to match on (amount or invoice number)."""
        return self.total_amount is not None or self.invoice_number is not None


class FireflyTransaction(BaseModel):
    """A transaction as returned by the Firefly III API (the fields we use)."""

    model_config = ConfigDict(frozen=True)

    id: str
    journal_id: str = Field(description="transaction_journal_id — what attachments bind to.")
    date: date
    amount: Decimal
    currency_code: str
    description: str
    source_iban: str | None = None
    destination_iban: str | None = None
    category_name: str | None = None
    tags: tuple[str, ...] = Field(default=(), description="Existing tags — preserved on write.")
    web_url: str = Field(description="Deep link into the Firefly UI for review.")


class FireflyAccount(BaseModel):
    """A Firefly account (the fields the importer/resolver use)."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    account_type: str
    iban: str | None = None
    currency_code: str = "EUR"
    account_role: str | None = None


class MatchOutcome(StrEnum):
    AUTO_ATTACHED = "auto_attached"
    ATTACHED_NEEDS_REVIEW = "attached_needs_review"
    NO_MATCH = "no_match"
    NOT_ACTIONABLE = "not_actionable"
    ERROR = "error"


class MatchResult(BaseModel):
    """The decision and action taken for one document — one row in the audit report."""

    invoice: ExtractedInvoice
    transaction: FireflyTransaction | None = None
    score: float = 0.0
    outcome: MatchOutcome = MatchOutcome.NO_MATCH
    detail: str = ""

    @property
    def transaction_web_url(self) -> str | None:
        return self.transaction.web_url if self.transaction else None
