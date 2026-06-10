"""Tests for Phase 1b correction capture (fakes only, no network)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from firefly_bot.enrich.corrections import capture_corrections
from firefly_bot.models import FireflyTransaction, LabelRecord


class FakeLabelStore:
    def __init__(self) -> None:
        self.records: list[LabelRecord] = []

    def record(self, record: LabelRecord) -> None:
        self.records.append(record)

    def close(self) -> None:
        return None


def _category_record(
    firefly_id: str | None,
    predicted: str | None,
    *,
    corrected: str | None = None,
) -> LabelRecord:
    return LabelRecord(
        ts=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        kind="category",
        features={"counterparty_name": "Albert Heijn", "firefly_id": firefly_id},
        predicted=predicted,
        score=1.0,
        corrected=corrected,
        source="auto",
    )


def _txn(firefly_id: str, category: str | None) -> FireflyTransaction:
    return FireflyTransaction(
        id=firefly_id,
        journal_id=f"j-{firefly_id}",
        date=date(2026, 6, 1),
        amount=Decimal("10.00"),
        currency_code="EUR",
        description="boodschappen",
        tx_type="withdrawal",
        category_name=category,
        web_url="https://example/transactions/show/" + firefly_id,
    )


def test_changed_category_produces_one_user_correction() -> None:
    prior = [_category_record("100", "Boodschappen")]
    current = {"100": _txn("100", "Eten buiten de deur")}
    store = FakeLabelStore()

    summary = capture_corrections(prior, current, store)

    assert summary.corrections == 1
    assert summary.unchanged == 0
    assert summary.checked == 1
    (rec,) = store.records
    assert rec.kind == "category"
    assert rec.predicted == "Boodschappen"  # the original prediction is preserved
    assert rec.corrected == "Eten buiten de deur"  # ground truth from Firefly
    assert rec.source == "user"
    assert rec.score == 1.0
    assert rec.features["firefly_id"] == "100"


def test_previously_uncategorised_now_categorised_is_a_correction() -> None:
    prior = [_category_record("101", None)]
    current = {"101": _txn("101", "Boodschappen")}
    store = FakeLabelStore()

    summary = capture_corrections(prior, current, store)

    assert summary.corrections == 1
    (rec,) = store.records
    assert rec.predicted is None
    assert rec.corrected == "Boodschappen"


def test_unchanged_category_writes_nothing() -> None:
    prior = [_category_record("200", "Boodschappen")]
    current = {"200": _txn("200", "Boodschappen")}
    store = FakeLabelStore()

    summary = capture_corrections(prior, current, store)

    assert summary.unchanged == 1
    assert summary.corrections == 0
    assert store.records == []


def test_cleared_category_is_not_recorded_as_a_none_correction() -> None:
    # User removed the category we set. Recording corrected=None would collide with the
    # "uncorrected" sentinel and re-emit every run, so this must write nothing and stay idempotent.
    prior = [_category_record("500", "Boodschappen")]
    current = {"500": _txn("500", None)}
    store = FakeLabelStore()

    summary = capture_corrections(prior, current, store)

    assert summary.corrections == 0
    assert summary.unchanged == 1
    assert store.records == []


def test_missing_firefly_id_is_unresolved() -> None:
    prior = [_category_record(None, "Boodschappen")]
    store = FakeLabelStore()

    summary = capture_corrections(prior, {}, store)

    assert summary.unresolved == 1
    assert summary.checked == 0
    assert store.records == []


def test_transaction_absent_from_firefly_is_unresolved() -> None:
    prior = [_category_record("300", "Boodschappen")]
    store = FakeLabelStore()

    summary = capture_corrections(prior, {}, store)  # id 300 not in current

    assert summary.unresolved == 1
    assert store.records == []


def test_rerun_with_correction_present_is_idempotent() -> None:
    # First pass produces a correction record; feed it back in as prior on the second pass.
    original = _category_record("400", "Boodschappen")
    current = {"400": _txn("400", "Eten buiten de deur")}

    first = FakeLabelStore()
    capture_corrections([original], current, first)
    (correction,) = first.records

    second = FakeLabelStore()
    summary = capture_corrections([original, correction], current, second)

    assert summary.corrections == 0  # already captured, not re-emitted
    assert second.records == []
