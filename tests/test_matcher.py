"""Unit tests for the matcher scoring/outcome logic (no network)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from firefly_bot.config import MatchingSettings
from firefly_bot.matching.matcher import match_invoice
from firefly_bot.models import (
    Attachment,
    ExtractedInvoice,
    FieldConfidence,
    FireflyTransaction,
    MatchOutcome,
)


def _attachment() -> Attachment:
    return Attachment(
        filename="invoice.pdf",
        content_type="application/pdf",
        data=b"x",
        sha256="deadbeef",
        source_message_id="<1@x>",
        received_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


def _invoice(*, iban: str | None) -> ExtractedInvoice:
    return ExtractedInvoice(
        source=_attachment(),
        total_amount=Decimal("121.00"),
        counterparty_iban=iban,
        invoice_date=date(2026, 5, 28),
        total_confidence=FieldConfidence.HIGH,
        iban_confidence=FieldConfidence.HIGH if iban else FieldConfidence.NONE,
    )


def _txn(iban: str | None) -> FireflyTransaction:
    return FireflyTransaction(
        id="42",
        journal_id="99",
        date=date(2026, 5, 30),
        amount=Decimal("-121.00"),
        currency_code="EUR",
        description="Acme",
        source_iban="NL00MINE0000000000",
        destination_iban=iban,
        web_url="https://ff/transactions/show/42",
    )


def test_iban_and_amount_match_auto_attaches() -> None:
    settings = MatchingSettings()
    result = match_invoice(_invoice(iban="NL91ABNA0417164300"),
                           [_txn("NL91ABNA0417164300")], settings)
    assert result.outcome is MatchOutcome.AUTO_ATTACHED
    assert result.transaction is not None and result.transaction.id == "42"


def test_amount_only_match_needs_review() -> None:
    settings = MatchingSettings()
    result = match_invoice(_invoice(iban=None), [_txn(None)], settings)
    assert result.outcome is MatchOutcome.ATTACHED_NEEDS_REVIEW


def test_no_amount_is_not_actionable() -> None:
    settings = MatchingSettings()
    inv = _invoice(iban=None).model_copy(update={"total_amount": None})
    result = match_invoice(inv, [_txn(None)], settings)
    assert result.outcome is MatchOutcome.NOT_ACTIONABLE
