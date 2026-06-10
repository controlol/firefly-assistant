"""Tests for the statement importer using a fake StatementWriter (no network)."""

from __future__ import annotations

from decimal import Decimal

from firefly_bot.banking.camt import BankStatement, BankTransaction
from firefly_bot.banking.importer import import_statement
from firefly_bot.models import FireflyAccount


class FakeWriter:
    def __init__(self, duplicate_dates: tuple[str, ...] = ()) -> None:
        self.created_opposing: list[tuple[str, str | None, str]] = []
        self.created_txns: list[dict[str, object]] = []
        self.duplicate_dates = set(duplicate_dates)
        self._next = 1000

    def list_accounts(self, account_type: str) -> list[FireflyAccount]:
        return []

    def ensure_asset_account(self, iban: str, currency: str, role: str, name: str) -> str:
        return f"asset-{iban[-4:]}"

    def create_opposing_account(self, name: str, iban: str | None, role: str) -> str:
        self._next += 1
        account_id = str(self._next)
        self.created_opposing.append((name, iban, role))
        return account_id

    def create_transaction(self, split: dict[str, object], *, skip_duplicates: bool) -> bool:
        if split["date"] in self.duplicate_dates:
            return False
        self.created_txns.append(split)
        return True


def _statement() -> BankStatement:
    return BankStatement(
        account_iban="NL00BANK0123456789",
        currency="EUR",
        transactions=[
            BankTransaction(
                date="2026-04-01", amount=Decimal("10.00"), is_outgoing=True,
                description="boodschappen", counterparty_name="Albert Heijn 2264",
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


def test_dedups_albert_heijn_into_one_expense_account() -> None:
    writer = FakeWriter()
    summary = import_statement(_statement(), writer, owner_name="J. Jansen")
    # Two AH variants -> a single expense account created and reused.
    expense = [c for c in writer.created_opposing if c[2] == "expense"]
    assert len(expense) == 1
    assert summary.created == 3


def test_owner_named_counterparty_becomes_transfer() -> None:
    writer = FakeWriter()
    summary = import_statement(_statement(), writer, owner_name="J. Jansen")
    assert summary.transfers == 1
    transfer = next(t for t in writer.created_txns if t["type"] == "transfer")
    assert transfer["destination_id"] == "asset-9613"  # money in -> our main account


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
