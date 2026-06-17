"""Tests for the statement importer using a fake StatementWriter (no network)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from firefly_bot.banking.camt import BankStatement, BankTransaction
from firefly_bot.banking.importer import import_statement
from firefly_bot.firefly.client import AssetAccountNotFoundError
from firefly_bot.labels import JsonlLabelStore
from firefly_bot.models import CategorySuggestion, FireflyAccount, LabelRecord


def _tags(split: dict[str, object]) -> list[str]:
    tags = split.get("tags")
    return tags if isinstance(tags, list) else []


class FakeLabelStore:
    def __init__(self) -> None:
        self.records: list[LabelRecord] = []

    def record(self, record: LabelRecord) -> None:
        self.records.append(record)

    def close(self) -> None:
        return None


class FakeWriter:
    def __init__(
        self,
        duplicate_dates: tuple[str, ...] = (),
        existing_asset_ibans: tuple[str, ...] = (),
        existing_opposing: tuple[FireflyAccount, ...] = (),
    ) -> None:
        self.created_opposing: list[tuple[str, str | None, str]] = []
        self.created_txns: list[dict[str, object]] = []
        self.duplicate_dates = set(duplicate_dates)
        self.existing_asset_ibans = set(existing_asset_ibans)
        self.existing_opposing = existing_opposing
        self._next = 1000

    def list_accounts(self, account_type: str) -> list[FireflyAccount]:
        return [a for a in self.existing_opposing if a.account_type == account_type]

    def ensure_asset_account(
        self,
        iban: str,
        currency: str,
        role: str,
        name: str,
        *,
        opening_balance: Decimal | None = None,
        opening_date: str | None = None,
        create: bool = True,
    ) -> str:
        if not create and iban not in self.existing_asset_ibans:
            raise AssetAccountNotFoundError(iban, name)
        return f"asset-{iban[-4:]}"

    def create_opposing_account(self, name: str, iban: str | None, role: str) -> str:
        self._next += 1
        account_id = str(self._next)
        self.created_opposing.append((name, iban, role))
        return account_id

    def create_transaction(
        self, split: dict[str, object], *, skip_duplicates: bool
    ) -> str | None:
        if split["date"] in self.duplicate_dates:
            return None
        self.created_txns.append(split)
        self._next += 1
        return str(self._next)


def _statement() -> BankStatement:
    return BankStatement(
        account_iban="NL00BANK0123456789",
        currency="EUR",
        transactions=[
            BankTransaction(
                date="2026-04-01", amount=Decimal("10.00"), is_outgoing=True,
                description="boodschappen", counterparty_name="Albert Heijn 2264", mcc="5411",
            ),
            BankTransaction(
                date="2026-04-02", amount=Decimal("12.00"), is_outgoing=True,
                description="boodschappen", counterparty_name="Albert Heijn 2277",
            ),
            BankTransaction(
                date="2026-04-03", amount=Decimal("500.00"), is_outgoing=False,
                description="van spaar", counterparty_name="J. Jansen",
                counterparty_iban="NL00BANK9876543210",
            ),
        ],
    )


def _collector_statement() -> BankStatement:
    iban = "NL04ADYB2017400157"  # Adyen collector IBAN: many merchants settle through one account
    return BankStatement(
        account_iban="NL00BANK0123456789",
        currency="EUR",
        transactions=[
            BankTransaction(
                date="2026-04-01", amount=Decimal("33.90"), is_outgoing=True,
                description="TFGSN7HWZJKMVNZ32K5B3 ref", counterparty_name="Zara.com",
                counterparty_iban=iban,
            ),
            BankTransaction(
                date="2026-04-02", amount=Decimal("331.60"), is_outgoing=True,
                description="GXPZ6XDG478K4VQ9JEAJ ref", counterparty_name="PaylogicHoldingBV",
                counterparty_iban=iban,
            ),
            BankTransaction(
                date="2026-04-03", amount=Decimal("10.00"), is_outgoing=True,
                description="boodschappen", counterparty_name="Albert Heijn",
                counterparty_iban="NL12ABNA0123456789",
            ),
        ],
    )


def test_collector_iban_keeps_one_account_and_records_merchant_in_description() -> None:
    writer = FakeWriter()
    summary = import_statement(_collector_statement(), writer)
    # The two Adyen merchants share one IBAN -> one shared expense account (Firefly enforces IBAN
    # uniqueness), plus one for Albert Heijn = 2 expense accounts, not 3.
    expense = [c for c in writer.created_opposing if c[2] == "expense"]
    assert len(expense) == 2
    # The shared account keeps the IBAN; the real merchant is preserved in the description instead.
    zara = next(t for t in writer.created_txns if t["amount"] == "33.90")
    paylogic = next(t for t in writer.created_txns if t["amount"] == "331.60")
    assert zara["description"] == "Zara.com — TFGSN7HWZJKMVNZ32K5B3 ref"
    assert paylogic["description"] == "PaylogicHoldingBV — GXPZ6XDG478K4VQ9JEAJ ref"
    # A normal single-merchant IBAN is left untouched (no merchant prefix added).
    ah = next(t for t in writer.created_txns if t["amount"] == "10.00")
    assert ah["description"] == "boodschappen"
    assert summary.created == 3


def test_collector_iban_detected_from_existing_firefly_account() -> None:
    # Firefly already holds "Zara.com" on the Adyen IBAN (from an earlier import). A new, single
    # transaction with a DIFFERENT name on that same IBAN must still be treated as a collector IBAN,
    # so the new merchant is recorded in the description and reuses the shared account by IBAN.
    existing = FireflyAccount(
        id="acc-zara", name="Zara.com", account_type="expense", iban="NL04ADYB2017400157",
    )
    writer = FakeWriter(existing_opposing=(existing,))
    statement = BankStatement(
        account_iban="NL00BANK0123456789",
        currency="EUR",
        transactions=[
            BankTransaction(
                date="2026-04-02", amount=Decimal("19.99"), is_outgoing=True,
                description="opaque-ref", counterparty_name="KARWEI",
                counterparty_iban="NL04ADYB2017400157",
            ),
        ],
    )
    import_statement(statement, writer)
    row = writer.created_txns[0]
    assert row["description"] == "KARWEI — opaque-ref"
    # Resolved to the existing shared account by IBAN; no new expense account created.
    assert writer.created_opposing == []
    assert row["destination_id"] == "acc-zara"


def test_dedups_albert_heijn_into_one_expense_account() -> None:
    writer = FakeWriter()
    summary = import_statement(_statement(), writer, owner_name="J. Jansen")
    # Two AH variants -> a single expense account created and reused.
    expense = [c for c in writer.created_opposing if c[2] == "expense"]
    assert len(expense) == 1
    assert summary.created == 3


def test_missing_asset_account_without_create_flag_raises() -> None:
    # No asset account carries the statement IBAN and create_account=False -> hard error instead of
    # silently creating a duplicate. (The CLI maps this to a clean exit; see --create-account.)
    writer = FakeWriter()  # no existing asset IBANs
    with pytest.raises(AssetAccountNotFoundError):
        import_statement(_statement(), writer, owner_name="J. Jansen", create_account=False)


def test_existing_asset_accounts_import_without_create_flag() -> None:
    # When the asset (and own-savings) IBANs already exist, create_account=False imports normally.
    writer = FakeWriter(existing_asset_ibans=("NL00BANK0123456789", "NL00BANK9876543210"))
    summary = import_statement(_statement(), writer, owner_name="J. Jansen", create_account=False)
    assert summary.created == 3


def test_owner_named_counterparty_becomes_transfer() -> None:
    writer = FakeWriter()
    summary = import_statement(_statement(), writer, owner_name="J. Jansen")
    assert summary.transfers == 1
    transfer = next(t for t in writer.created_txns if t["type"] == "transfer")
    assert transfer["destination_id"] == "asset-6789"  # money in -> our main account


def test_duplicates_are_counted_not_created() -> None:
    writer = FakeWriter(duplicate_dates=("2026-04-02",))
    summary = import_statement(_statement(), writer, owner_name="J. Jansen")
    assert summary.duplicates == 1
    assert summary.created == 2


def test_dry_run_writes_nothing() -> None:
    writer = FakeWriter()
    summary = import_statement(_statement(), writer, owner_name="J. Jansen", dry_run=True)
    assert summary.created == 3
    assert writer.created_txns == []
    assert writer.created_opposing == []


def test_mcc_sets_category_on_card_payment() -> None:
    writer = FakeWriter()
    import_statement(_statement(), writer, owner_name="J. Jansen")
    albert_heijn = next(t for t in writer.created_txns if t["amount"] == "10.00")
    assert albert_heijn["category_name"] == "Boodschappen"  # MCC 5411
    # The transfer must not be categorised.
    transfer = next(t for t in writer.created_txns if t["type"] == "transfer")
    assert "category_name" not in transfer


def test_emits_category_and_merchant_labels_per_transaction() -> None:
    writer = FakeWriter()
    store = FakeLabelStore()
    import_statement(
        _statement(), writer, owner_name="J. Jansen", label_store=store
    )
    categories = [r for r in store.records if r.kind == "category"]
    merchants = [r for r in store.records if r.kind == "merchant"]
    # One of each per transaction (3 transactions).
    assert len(categories) == 3
    assert len(merchants) == 3
    # The MCC-tagged Albert Heijn row predicts a category; features carry the raw inputs.
    ah = next(r for r in categories if r.features["mcc"] == "5411")
    assert ah.predicted == "Boodschappen"
    assert ah.features["counterparty_name"] == "Albert Heijn 2264"
    # The two AH variants normalise to the same merchant key (dedup signal for Phase 2).
    ah_merchants = [r for r in merchants if "albert" in str(r.features["merchant_key"])]
    assert {r.features["merchant_key"] for r in ah_merchants} == {"albert heijn"}
    assert all(r.source == "auto" and r.corrected is None for r in store.records)
    # Phase 1b: every record carries the id Firefly assigned the created transaction, so a later
    # correction pass can key the prediction back to its transaction.
    assert all(isinstance(r.features["firefly_id"], str) for r in store.records)
    # The category + merchant pair for one transaction share that transaction's id.
    ah_cat = next(r for r in categories if r.features["counterparty_name"] == "Albert Heijn 2264")
    ah_mer = next(r for r in merchants if r.features["counterparty_name"] == "Albert Heijn 2264")
    assert ah_cat.features["firefly_id"] == ah_mer.features["firefly_id"]


class FakeCategoriser:
    """Tiny stub returning canned suggestions per counterparty — no model, no network.

    Mirrors the two methods the importer calls on the real `Categoriser`: `suggest` returns a
    pre-seeded `CategorySuggestion` (default: none/needs-review), and `is_auto` applies the same
    confidence policy as the real one (mcc always; knn >= knn_trust; zeroshot >= gate).
    """

    def __init__(
        self,
        suggestions: dict[str, CategorySuggestion],
        *,
        gate: float = 0.83,
        knn_trust: float = 0.90,
    ) -> None:
        self._suggestions = suggestions
        self._gate = gate
        self._knn_trust = knn_trust
        self.calls: list[tuple[str, str, str | None]] = []

    def suggest(
        self, counterparty_name: str, description: str, mcc: str | None
    ) -> CategorySuggestion:
        self.calls.append((counterparty_name, description, mcc))
        return self._suggestions.get(
            counterparty_name,
            CategorySuggestion(label=None, confidence=0.0, provenance="none"),
        )

    def is_auto(self, s: CategorySuggestion) -> bool:
        if s.provenance == "mcc":
            return True
        if s.provenance == "knn":
            return s.confidence >= self._knn_trust
        if s.provenance == "zeroshot":
            return s.confidence >= self._gate
        return False


def test_confident_suggestion_sets_category_name() -> None:
    writer = FakeWriter()
    categoriser = FakeCategoriser(
        {
            "Albert Heijn 2277": CategorySuggestion(
                label="Boodschappen", confidence=0.95, provenance="knn", evidence="Albert Heijn"
            )
        }
    )
    import_statement(
        _statement(),
        writer,
        owner_name="J. Jansen",
        categoriser=categoriser,  # type: ignore[arg-type]  # structural stub, not a subclass
    )
    row = next(t for t in writer.created_txns if t["amount"] == "12.00")
    assert row["category_name"] == "Boodschappen"
    assert "needs-review" not in _tags(row)


def test_weak_suggestion_leaves_category_unset_and_flags_review() -> None:
    writer = FakeWriter()
    categoriser = FakeCategoriser(
        {
            "Albert Heijn 2277": CategorySuggestion(
                label="Boodschappen", confidence=0.50, provenance="knn", evidence="x"
            )
        }
    )
    import_statement(
        _statement(),
        writer,
        owner_name="J. Jansen",
        categoriser=categoriser,  # type: ignore[arg-type]  # structural stub, not a subclass
        needs_review_tag="needs-review",
    )
    row = next(t for t in writer.created_txns if t["amount"] == "12.00")
    assert "category_name" not in row  # weak k-NN -> not auto-applied
    assert "needs-review" in _tags(row)  # routed to review


def test_category_label_record_carries_provenance_and_confidence() -> None:
    writer = FakeWriter()
    store = FakeLabelStore()
    categoriser = FakeCategoriser(
        {
            "Albert Heijn 2277": CategorySuggestion(
                label="Boodschappen",
                confidence=0.88,
                provenance="zeroshot",
                evidence="Boodschappen",
            )
        }
    )
    import_statement(
        _statement(),
        writer,
        owner_name="J. Jansen",
        label_store=store,
        categoriser=categoriser,  # type: ignore[arg-type]  # structural stub, not a subclass
    )
    rec = next(
        r
        for r in store.records
        if r.kind == "category" and r.features["counterparty_name"] == "Albert Heijn 2277"
    )
    assert rec.predicted == "Boodschappen"
    assert rec.score == 0.88
    assert rec.features["provenance"] == "zeroshot"


def test_transfers_are_never_categorised_with_a_categoriser() -> None:
    writer = FakeWriter()
    # Even if the stub would return a confident suggestion, transfers must stay uncategorised and
    # the categoriser must not even be consulted for them.
    categoriser = FakeCategoriser(
        {
            "J. Jansen": CategorySuggestion(
                label="Boodschappen", confidence=1.0, provenance="knn", evidence="x"
            )
        }
    )
    import_statement(
        _statement(),
        writer,
        owner_name="J. Jansen",
        categoriser=categoriser,  # type: ignore[arg-type]  # structural stub, not a subclass
    )
    transfer = next(t for t in writer.created_txns if t["type"] == "transfer")
    assert "category_name" not in transfer
    assert ("J. Jansen", "van spaar", None) not in categoriser.calls


def test_none_categoriser_is_byte_identical_to_mcc_only() -> None:
    baseline = FakeWriter()
    import_statement(_statement(), baseline, owner_name="J. Jansen")
    with_none = FakeWriter()
    import_statement(_statement(), with_none, owner_name="J. Jansen", categoriser=None)
    assert with_none.created_txns == baseline.created_txns


def test_jsonl_label_store_round_trips_a_record(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "labels.jsonl"  # parent dir does not exist yet
    store = JsonlLabelStore(path)
    rec = LabelRecord(
        ts=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
        kind="category",
        features={"mcc": "5411", "amount_delta": 0.0, "iban_match": True, "missing": None},
        predicted="Boodschappen",
        score=1.0,
        source="auto",
    )
    store.record(rec)
    store.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert LabelRecord.model_validate_json(lines[0]) == rec
