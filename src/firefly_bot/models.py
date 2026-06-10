"""Typed domain models shared across the pipeline.

Everything that crosses a module boundary is one of these pydantic models, so the whole
pipeline is statically checkable with `mypy --strict`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Literal

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

    # Files to attach to the matched transaction. Empty means "just the source"; it is populated
    # when extraction and attachment differ — e.g. read data from a UBL but always attach the PDF.
    documents: tuple[Attachment, ...] = ()

    # Per-field confidence so the matcher and report can reason about reliability.
    total_confidence: FieldConfidence = FieldConfidence.NONE
    iban_confidence: FieldConfidence = FieldConfidence.NONE
    number_confidence: FieldConfidence = FieldConfidence.NONE
    date_confidence: FieldConfidence = FieldConfidence.NONE

    @property
    def is_actionable(self) -> bool:
        """True when we have at least one identifier to match on (amount or invoice number)."""
        return self.total_amount is not None or self.invoice_number is not None

    @property
    def attachables(self) -> tuple[Attachment, ...]:
        """The document(s) to upload to Firefly — the explicit set, or just the source."""
        return self.documents or (self.source,)


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
    source_name: str | None = None
    destination_name: str | None = None
    tx_type: str = Field(default="", description="Firefly split type: withdrawal/deposit/transfer.")
    category_name: str | None = None
    tags: tuple[str, ...] = Field(default=(), description="Existing tags — preserved on write.")
    web_url: str = Field(description="Deep link into the Firefly UI for review.")

    @property
    def is_outgoing(self) -> bool:
        """True when money leaves the account (a withdrawal).

        Direction comes from the Firefly split ``type``: the API returns *positive* amounts and
        signals direction with the type, so the amount sign is unreliable. Falls back to the sign
        only when ``tx_type`` is absent (e.g. hand-built fixtures), so older callers keep working.
        """
        if self.tx_type:
            return self.tx_type == "withdrawal"
        return self.amount < 0

    @property
    def counterparty_name(self) -> str | None:
        """The other party's name: destination when outgoing (payee), else source (payer).

        Mirrors the embedded categoriser text "{counterparty_name} {description}".
        """
        return self.destination_name if self.is_outgoing else self.source_name

    @property
    def counterparty_iban(self) -> str | None:
        """The other party's IBAN, by direction — destination when outgoing, else source."""
        return self.destination_iban if self.is_outgoing else self.source_iban


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


class LabelRecord(BaseModel):
    """One append-only training example: a decision the pipeline made (+ later, its correction).

    Phase 1 only accumulates signal — there is no model yet. Every auto-tag, category, and match
    candidate the pipeline considers is logged as a `LabelRecord` so later phases can re-featurise
    the raw inputs and learn from them. `corrected` stays None until Phase 1b reads Firefly state.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: int = 1
    ts: datetime
    kind: Literal["match", "category", "merchant"]
    # Raw inputs, so a future model can re-featurise without re-running the pipeline.
    features: dict[str, str | float | bool | None]
    # What we decided automatically, and the confidence in that decision.
    predicted: str | None
    score: float
    # Ground truth, filled in later from a Firefly state diff (None until known).
    corrected: str | None = None
    source: Literal["auto", "user"]


class CandidateLabel(BaseModel):
    """A *new* category the data is asking for: a recurring cluster of orphan transactions.

    Phase 2.2 turns the below-the-gate "orphan" pile (transactions the categoriser could not
    place, see docs/COLD_START.md step 4) into candidate new categories. A cluster of similar
    orphans large enough to be recurring (``size >= min_size``) is evidence that a category is
    missing; it is named heuristically and surfaced for one-click accept/rename/reject. On accept
    its ``member_texts`` become labelled examples that anchor future runs.

    - ``suggested_name``: heuristic name from the cluster's dominant merchant / shared token.
    - ``member_texts``: the orphan texts that formed the cluster (the future examples).
    - ``size``: number of members (the recurrence/impact signal; clusters sort by this desc).
    - ``cohesion``: mean intra-cluster cosine similarity in [0, 1] — how tight the cluster is.
    """

    model_config = ConfigDict(frozen=True)

    suggested_name: str
    member_texts: list[str]
    size: int
    cohesion: float


class CategorySuggestion(BaseModel):
    """The enricher's category proposal for one transaction, with its provenance.

    Provenance records *why* a label was applied so the decision is auditable and a later scorer
    can weight sources differently (see docs/COLD_START.md step 5):

    - ``mcc``: deterministic MCC->category map (high precision, no ML, confidence 1.0).
    - ``knn``: nearest labelled example cleared the gate; ``evidence`` is that example's text.
    - ``zeroshot``: nearest label *name* cleared the gate; ``evidence`` is that label name.
    - ``none``: nothing cleared the gate — the caller should mark the txn ``needs-review``.
    """

    model_config = ConfigDict(frozen=True)

    label: str | None
    confidence: float
    provenance: Literal["mcc", "knn", "zeroshot", "none"]
    evidence: str | None = None
