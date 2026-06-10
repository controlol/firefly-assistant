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


def _invoice(
    *, iban: str | None = None, number: str | None = None, total: Decimal | None = Decimal("121.00")
) -> ExtractedInvoice:
    return ExtractedInvoice(
        source=_attachment(),
        total_amount=total,
        counterparty_iban=iban,
        invoice_number=number,
        invoice_date=date(2026, 5, 28),
        total_confidence=FieldConfidence.HIGH if total else FieldConfidence.NONE,
        iban_confidence=FieldConfidence.HIGH if iban else FieldConfidence.NONE,
        number_confidence=FieldConfidence.HIGH if number else FieldConfidence.NONE,
    )


def _txn(*, iban: str | None = None, description: str = "Acme") -> FireflyTransaction:
    return FireflyTransaction(
        id="42",
        journal_id="99",
        date=date(2026, 5, 30),
        amount=Decimal("-121.00"),
        currency_code="EUR",
        description=description,
        source_iban="NL00MINE0000000000",
        destination_iban=iban,
        web_url="https://ff/transactions/show/42",
    )


def test_number_in_reference_plus_amount_auto_attaches() -> None:
    result = match_invoice(
        _invoice(number="F26000352"),
        [_txn(description="Betaling factuur F26000352")],
        MatchingSettings(),
    )
    assert result.outcome is MatchOutcome.AUTO_ATTACHED
    assert result.transaction is not None and result.transaction.id == "42"


def test_amount_and_iban_without_number_needs_review() -> None:
    # Strong but not definitive: no invoice number in the reference -> not auto.
    result = match_invoice(
        _invoice(iban="NL91ABNA0417164300"),
        [_txn(iban="NL91ABNA0417164300")],
        MatchingSettings(),
    )
    assert result.outcome is MatchOutcome.ATTACHED_NEEDS_REVIEW


def test_number_match_but_wrong_amount_needs_review() -> None:
    result = match_invoice(
        _invoice(number="F26000352", total=Decimal("999.99")),
        [_txn(description="factuur F26000352")],
        MatchingSettings(),
    )
    assert result.outcome is MatchOutcome.ATTACHED_NEEDS_REVIEW


def test_short_number_is_not_used_as_a_match() -> None:
    # A 3-char number could collide; it must not drive a match on its own.
    result = match_invoice(
        _invoice(number="A12", total=Decimal("5.00")),
        [_txn(description="contains A12 somewhere")],
        MatchingSettings(),
    )
    assert result.outcome is MatchOutcome.NO_MATCH


def test_no_identifiers_is_not_actionable() -> None:
    result = match_invoice(_invoice(total=None), [_txn()], MatchingSettings())
    assert result.outcome is MatchOutcome.NOT_ACTIONABLE


def test_no_candidate_yields_no_match() -> None:
    result = match_invoice(_invoice(total=Decimal("999.99")), [_txn()], MatchingSettings())
    assert result.outcome is MatchOutcome.NO_MATCH
