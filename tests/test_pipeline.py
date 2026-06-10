"""End-to-end pipeline tests using injected fakes — no network, OCR, or filesystem services.

Demonstrates the DI payoff: the whole orchestration (including the auto-write path) is
exercised without any real Firefly, inbox, or PaddleOCR.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal

from firefly_bot.config import FireflySettings, ImapSettings, MatchingSettings, Settings
from firefly_bot.firefly.ledger import DryRunLedger
from firefly_bot.models import Attachment, FireflyTransaction, MatchResult
from firefly_bot.pipeline import run

# --- fakes -------------------------------------------------------------------

class FakeSource:
    def __init__(self, attachments: list[Attachment]) -> None:
        self._attachments = attachments

    def fetch(self) -> list[Attachment]:
        return self._attachments


class FakeRecogniser:
    def __init__(self, text: str) -> None:
        self._text = text

    def to_text(self, attachment: Attachment) -> str:
        return self._text


class FakeLedger:
    def __init__(self, transactions: list[FireflyTransaction]) -> None:
        self._transactions = transactions
        self.attached: list[tuple[str, str]] = []
        self.tagged: list[tuple[str, list[str]]] = []

    def list_transactions(self, *, start: date, end: date) -> list[FireflyTransaction]:
        return [t for t in self._transactions if start <= t.date <= end]

    def attach_document(self, transaction: FireflyTransaction, attachment: Attachment) -> str:
        self.attached.append((transaction.journal_id, attachment.filename))
        return "fake-id"

    def add_tags(self, transaction: FireflyTransaction, tags: list[str]) -> None:
        self.tagged.append((transaction.id, list(tags)))

    def close(self) -> None:
        return None


class CapturingReportWriter:
    def __init__(self) -> None:
        self.results: list[MatchResult] = []

    def write(self, results: list[MatchResult], report_dir: str) -> str:
        self.results = results
        return f"{report_dir}/fake-report.xlsx"


# --- fixtures-as-helpers -----------------------------------------------------

def _settings() -> Settings:
    return Settings(
        imap=ImapSettings(host="h", username="u", password="p"),
        firefly=FireflySettings(base_url="http://f", token="t"),
        matching=MatchingSettings(),
        report_dir=tempfile.mkdtemp(),
    )


def _attachment() -> Attachment:
    return Attachment(
        filename="invoice.pdf",
        content_type="application/pdf",
        data=b"%PDF-fake",
        sha256="hash-1",
        source_message_id="<1@x>",
        received_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


def _transaction() -> FireflyTransaction:
    return FireflyTransaction(
        id="42",
        journal_id="99",
        date=date(2026, 5, 30),
        amount=Decimal("-121.00"),
        currency_code="EUR",
        description="Betaling factuur F26000352",
        source_iban="NL00MINE0000000000",
        destination_iban="NL91ABNA0417164300",
        web_url="http://f/transactions/show/42",
    )


_MATCHING_TEXT = (
    "Factuurnummer F26000352\nTotaal te betalen  EUR 121,00\nIBAN NL91 ABNA 0417 1643 00"
)


# --- tests -------------------------------------------------------------------

def test_pipeline_auto_attaches_and_tags_matching_invoice() -> None:
    ledger = FakeLedger([_transaction()])
    report = CapturingReportWriter()
    run(
        _settings(),
        source=FakeSource([_attachment()]),
        recogniser=FakeRecogniser(_MATCHING_TEXT),
        ledger=ledger,
        report_writer=report,
    )
    assert ledger.attached == [("99", "invoice.pdf")]
    # IBAN + amount match -> auto-attach, only the processed tag (no needs-review).
    assert ledger.tagged == [("42", ["firefly-bot"])]
    assert report.results[0].outcome.value == "auto_attached"


def test_pipeline_dry_run_suppresses_writes() -> None:
    inner = FakeLedger([_transaction()])
    dry = DryRunLedger(inner)
    run(
        _settings(),
        source=FakeSource([_attachment()]),
        recogniser=FakeRecogniser(_MATCHING_TEXT),
        ledger=dry,
        report_writer=CapturingReportWriter(),
    )
    # Intended actions are recorded on the dry-run ledger...
    assert dry.attached == [("99", "invoice.pdf")]
    assert dry.tagged == [("42", ["firefly-bot"])]
    # ...but nothing reached the real (inner) ledger.
    assert inner.attached == []
    assert inner.tagged == []


def test_pipeline_no_match_writes_nothing() -> None:
    ledger = FakeLedger([_transaction()])
    report = CapturingReportWriter()
    run(
        _settings(),
        source=FakeSource([_attachment()]),
        recogniser=FakeRecogniser("Totaal te betalen  EUR 9,99"),  # amount won't match
        ledger=ledger,
        report_writer=report,
    )
    assert ledger.attached == []
    assert report.results[0].outcome.value == "no_match"
