"""End-to-end pipeline tests using injected fakes — no network, OCR, or filesystem services.

Demonstrates the DI payoff: the whole orchestration (including the auto-write path) is
exercised without any real Firefly, inbox, or PaddleOCR.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from firefly_bot.config import FireflySettings, ImapSettings, MatchingSettings, Settings
from firefly_bot.firefly.ledger import DryRunLedger
from firefly_bot.labels import NullLabelStore
from firefly_bot.models import Attachment, FireflyTransaction, LabelRecord, MatchResult
from firefly_bot.pipeline import run

# --- fakes -------------------------------------------------------------------

class FakeSource:
    def __init__(self, attachments: list[Attachment]) -> None:
        self._attachments = attachments
        self.processed: list[str] = []
        self.flagged: list[str] = []
        self.closed = False

    def fetch(self) -> list[Attachment]:
        return self._attachments

    def mark_processed(self, attachment: Attachment) -> None:
        self.processed.append(attachment.sha256)

    def flag_unprocessed(self, attachment: Attachment) -> None:
        self.flagged.append(attachment.sha256)

    def close(self) -> None:
        self.closed = True


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


class FakeLabelStore:
    def __init__(self) -> None:
        self.records: list[LabelRecord] = []
        self.closed = False

    def record(self, record: LabelRecord) -> None:
        self.records.append(record)

    def close(self) -> None:
        self.closed = True


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
        data_dir=tempfile.mkdtemp(),  # keep label writes out of the repo's ./data
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


def test_pipeline_preserves_existing_tags() -> None:
    txn = _transaction().model_copy(update={"tags": ("bot-fixture", "vakantie")})
    ledger = FakeLedger([txn])
    run(
        _settings(),
        source=FakeSource([_attachment()]),
        recogniser=FakeRecogniser(_MATCHING_TEXT),
        ledger=ledger,
        report_writer=CapturingReportWriter(),
    )
    # Existing tags kept; the tool's tag appended (no duplicates, no loss).
    assert ledger.tagged == [("42", ["bot-fixture", "vakantie", "firefly-bot"])]


def test_pipeline_no_match_writes_nothing() -> None:
    ledger = FakeLedger([_transaction()])
    report = CapturingReportWriter()
    source = FakeSource([_attachment()])
    run(
        _settings(),
        source=source,
        recogniser=FakeRecogniser("Totaal te betalen  EUR 9,99"),  # amount won't match
        ledger=ledger,
        report_writer=report,
    )
    assert ledger.attached == []
    assert report.results[0].outcome.value == "no_match"
    # Unmatched -> NOT processed (retried) but flagged for manual review; source still closed.
    assert source.processed == []
    assert source.flagged == ["hash-1"]
    assert source.closed is True


def _decoy_transaction() -> FireflyTransaction:
    # Same amount as the invoice but no invoice number / IBAN match -> a positive *loser*.
    return FireflyTransaction(
        id="7",
        journal_id="70",
        date=date(2026, 5, 28),
        amount=Decimal("-121.00"),
        currency_code="EUR",
        description="Andere betaling zonder factuurnummer",
        web_url="http://f/transactions/show/7",
    )


def test_pipeline_emits_one_match_record_per_candidate_with_chosen_flags() -> None:
    store = FakeLabelStore()
    run(
        _settings(),
        source=FakeSource([_attachment()]),
        recogniser=FakeRecogniser(_MATCHING_TEXT),
        ledger=FakeLedger([_transaction(), _decoy_transaction()]),
        report_writer=CapturingReportWriter(),
        label_store=store,
    )
    matches = [r for r in store.records if r.kind == "match"]
    # Both transactions score positively (amount matches), so both are captured.
    assert {r.features["candidate_id"] for r in matches} == {"42", "7"}
    chosen = [r for r in matches if r.features["chosen"] is True]
    negatives = [r for r in matches if r.features["chosen"] is False]
    # Exactly one winner, at least one negative for Phase 3 to train on.
    assert len(chosen) == 1
    assert chosen[0].features["candidate_id"] == "42"
    assert len(negatives) >= 1
    # `predicted` is the chosen id on every record; `corrected` stays None in Phase 1.
    assert all(r.predicted == "42" for r in matches)
    assert all(r.corrected is None and r.source == "auto" for r in matches)
    # The winner scores strictly higher than the negative (number + iban corroborators).
    assert chosen[0].score > negatives[0].score


def test_pipeline_dry_run_uses_null_label_store_and_writes_nothing(tmp_path: Path) -> None:
    settings = _settings().model_copy(update={"data_dir": str(tmp_path)})
    inner = FakeLedger([_transaction()])
    run(
        settings,
        source=FakeSource([_attachment()]),
        recogniser=FakeRecogniser(_MATCHING_TEXT),
        ledger=DryRunLedger(inner),
        report_writer=CapturingReportWriter(),
        dry_run=True,
    )
    # NullLabelStore is selected on dry-run -> no labels.jsonl is created anywhere under data_dir.
    assert not (tmp_path / "labels.jsonl").exists()


def test_null_label_store_is_a_noop() -> None:
    store = NullLabelStore()
    store.record(
        LabelRecord(ts=datetime.now(tz=UTC), kind="match", features={}, predicted=None, score=0.0,
                    source="auto")
    )
    store.close()  # must not raise


def test_pipeline_marks_source_processed_when_attached() -> None:
    source = FakeSource([_attachment()])
    run(
        _settings(),
        source=source,
        recogniser=FakeRecogniser(_MATCHING_TEXT),
        ledger=FakeLedger([_transaction()]),
        report_writer=CapturingReportWriter(),
    )
    assert source.processed == ["hash-1"]  # attached -> marked processed
    assert source.flagged == []  # ...and not flagged
