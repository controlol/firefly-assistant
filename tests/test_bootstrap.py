"""Unit tests for the bootstrap importer (cold-start booster), using fakes (no network).

A small list of `FireflyTransaction`s — categorised + uncategorised, outgoing + incoming — is run
through `bootstrap_labels` against an in-memory fake `LabelStore`. We assert the seed records, the
direction-derived counterparty name, idempotency on re-run, and the summary counts.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from firefly_bot.enrich.bootstrap import bootstrap_labels
from firefly_bot.models import FireflyTransaction, LabelRecord


class FakeLabelStore:
    """In-memory `LabelStore`: keeps every recorded `LabelRecord` for assertions."""

    def __init__(self) -> None:
        self.records: list[LabelRecord] = []

    def record(self, record: LabelRecord) -> None:
        self.records.append(record)

    def close(self) -> None:
        return None


def _tx(
    txn_id: str,
    *,
    amount: str,
    description: str,
    source_name: str | None = None,
    destination_name: str | None = None,
    source_iban: str | None = None,
    destination_iban: str | None = None,
    category_name: str | None = None,
    tx_type: str = "",
) -> FireflyTransaction:
    return FireflyTransaction(
        id=txn_id,
        journal_id=f"j{txn_id}",
        date=date(2026, 1, 1),
        amount=Decimal(amount),
        currency_code="EUR",
        description=description,
        source_name=source_name,
        destination_name=destination_name,
        source_iban=source_iban,
        destination_iban=destination_iban,
        category_name=category_name,
        tx_type=tx_type,
        web_url=f"https://firefly.example/transactions/show/{txn_id}",
    )


def _transactions() -> list[FireflyTransaction]:
    return [
        # Outgoing (amount < 0): counterparty is the destination (payee).
        _tx(
            "1",
            amount="-42.50",
            description="Boodschappen",
            source_name="Mijn Betaalrekening",
            destination_name="Albert Heijn 2264",
            destination_iban="NL00AHAH0000000001",
            category_name="Boodschappen",
        ),
        # Incoming (amount > 0): counterparty is the source (payer).
        _tx(
            "2",
            amount="1500.00",
            description="Salaris januari",
            source_name="Werkgever B.V.",
            destination_name="Mijn Betaalrekening",
            source_iban="NL00WERK0000000002",
            category_name="Inkomen",
        ),
        # Categorised but no counterparty name -> category record only, no merchant record.
        _tx(
            "3",
            amount="-9.99",
            description="Onbekende afschrijving",
            category_name="Overig",
        ),
        # Uncategorised -> skipped entirely.
        _tx(
            "4",
            amount="-12.00",
            description="Iets ongecategoriseerd",
            destination_name="Random Shop",
        ),
    ]


def test_categorised_emit_category_and_merchant_with_source_user() -> None:
    store = FakeLabelStore()
    bootstrap_labels(_transactions(), store)

    categories = [r for r in store.records if r.kind == "category"]
    merchants = [r for r in store.records if r.kind == "merchant"]

    # 3 categorised txns -> 3 category records; only 2 have a counterparty name -> 2 merchant.
    assert len(categories) == 3
    assert len(merchants) == 2
    assert all(r.source == "user" for r in store.records)
    assert all(r.score == 1.0 for r in store.records)

    ah = next(r for r in categories if r.features["counterparty_name"] == "Albert Heijn 2264")
    assert ah.predicted == "Boodschappen"
    assert ah.features["description"] == "Boodschappen"

    ah_merchant = next(
        r for r in merchants if r.features["counterparty_name"] == "Albert Heijn 2264"
    )
    assert ah_merchant.predicted == "Albert Heijn 2264"
    assert ah_merchant.features["counterparty_iban"] == "NL00AHAH0000000001"


def test_counterparty_name_derived_by_direction() -> None:
    store = FakeLabelStore()
    bootstrap_labels(_transactions(), store)
    names = {r.features["counterparty_name"] for r in store.records if r.kind == "category"}
    # Outgoing -> destination_name; incoming -> source_name; "Mijn Betaalrekening" never appears.
    assert "Albert Heijn 2264" in names  # outgoing -> destination
    assert "Werkgever B.V." in names  # incoming -> source
    assert "Mijn Betaalrekening" not in names


def test_direction_uses_type_over_amount_sign() -> None:
    # The real Firefly API returns POSITIVE amounts and signals direction via `type`. A withdrawal
    # with a positive amount must still resolve the counterparty to the destination (the payee),
    # not the account holder's own source account.
    store = FakeLabelStore()
    txn = _tx(
        "9",
        amount="42.50",  # positive, exactly as the API returns for a withdrawal
        tx_type="withdrawal",
        description="Boodschappen",
        source_name="Mijn Betaalrekening",
        destination_name="Albert Heijn 2264",
        category_name="Boodschappen",
    )
    bootstrap_labels([txn], store)
    names = {r.features["counterparty_name"] for r in store.records if r.kind == "category"}
    assert names == {"Albert Heijn 2264"}  # destination, despite the positive amount


def test_uncategorised_skipped() -> None:
    store = FakeLabelStore()
    bootstrap_labels(_transactions(), store)
    descriptions = {r.features["description"] for r in store.records if r.kind == "category"}
    assert "Iets ongecategoriseerd" not in descriptions


def test_summary_counts() -> None:
    store = FakeLabelStore()
    summary = bootstrap_labels(_transactions(), store)
    assert summary.scanned == 4
    assert summary.categorised == 3
    assert summary.category_records == 3
    assert summary.merchant_records == 2
    assert summary.skipped_duplicates == 0


def test_idempotent_rerun_emits_nothing_new() -> None:
    first = FakeLabelStore()
    bootstrap_labels(_transactions(), first)

    # Re-run passing the first run's records as `existing` -> every record is a duplicate.
    second = FakeLabelStore()
    summary = bootstrap_labels(_transactions(), second, existing=first.records)

    assert second.records == []
    assert summary.category_records == 0
    assert summary.merchant_records == 0
    assert summary.skipped_duplicates == len(first.records)


def test_idempotent_within_single_run_dedupes_repeats() -> None:
    # The same categorised merchant twice in one window must seed each record only once.
    txns = [_transactions()[0], _transactions()[0]]
    store = FakeLabelStore()
    summary = bootstrap_labels(txns, store)
    assert summary.category_records == 1
    assert summary.merchant_records == 1
    assert summary.skipped_duplicates == 2  # the second txn's category + merchant
