"""Import a parsed CAMT statement into Firefly III.

Depends only on the `StatementWriter` protocol, so it runs against a fake in tests. Idempotency
is delegated to Firefly's duplicate-hash detection (skip_duplicates), so re-importing the same
or overlapping statements does not create duplicate transactions.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel

from firefly_bot.banking.accounts import AccountResolver
from firefly_bot.banking.camt import BankStatement
from firefly_bot.banking.mcc import category_for_mcc
from firefly_bot.models import FireflyAccount

log = logging.getLogger("firefly_bot.import")


class StatementWriter(Protocol):
    def list_accounts(self, account_type: str) -> list[FireflyAccount]: ...
    def ensure_asset_account(
        self,
        iban: str,
        currency: str,
        role: str,
        name: str,
        *,
        opening_balance: Decimal | None = None,
        opening_date: str | None = None,
    ) -> str: ...
    def create_opposing_account(self, name: str, iban: str | None, role: str) -> str: ...
    def create_transaction(self, split: dict[str, object], *, skip_duplicates: bool) -> bool: ...


class ImportSummary(BaseModel):
    total: int
    created: int
    duplicates: int
    errors: int
    transfers: int
    asset_account_id: str
    savings_account_ids: dict[str, str]


def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def import_statement(
    statement: BankStatement,
    writer: StatementWriter,
    *,
    owner_name: str | None = None,
    own_ibans: frozenset[str] = frozenset(),
    account_name: str = "Betaalrekening",
    extra_tags: tuple[str, ...] = (),
    skip_duplicates: bool = True,
    dry_run: bool = False,
) -> ImportSummary:
    own = _own_accounts(statement, owner_name, own_ibans)

    asset_id = "" if dry_run else writer.ensure_asset_account(
        statement.account_iban, statement.currency, "defaultAsset",
        f"{account_name} {statement.account_iban[-6:]}",
        opening_balance=statement.opening_balance,
        opening_date=statement.opening_date or _earliest_date(statement),
    )
    savings = {} if dry_run else {
        iban: writer.ensure_asset_account(
            iban, statement.currency, "savingAsset", f"Spaarrekening {iban[-6:]}"
        )
        for iban in sorted(own)
    }

    opposing = _resolve_opposing_accounts(statement, writer, set(savings), dry_run)

    created = duplicates = errors = transfers = 0
    for index, tx in enumerate(statement.transactions):
        is_transfer = tx.counterparty_iban in savings
        split: dict[str, object] = {
            "date": tx.date,
            "amount": str(tx.amount),
            "description": tx.description,
            "tags": list(extra_tags),
        }
        if tx.reference:
            split["external_id"] = tx.reference[:255]
        category = None if is_transfer else category_for_mcc(tx.mcc)
        if category:
            split["category_name"] = category

        if is_transfer:
            assert tx.counterparty_iban is not None  # implied by is_transfer
            split["type"] = "transfer"
            other = savings[tx.counterparty_iban]
            src, dst = (asset_id, other) if tx.is_outgoing else (other, asset_id)
            split["source_id"], split["destination_id"] = src, dst
        elif tx.is_outgoing:
            split["type"] = "withdrawal"
            split["source_id"], split["destination_id"] = asset_id, opposing[index]
        else:
            split["type"] = "deposit"
            split["source_id"], split["destination_id"] = opposing[index], asset_id

        if dry_run:
            created += 1
            transfers += int(is_transfer)
            continue
        try:
            if writer.create_transaction(split, skip_duplicates=skip_duplicates):
                created += 1
                transfers += int(is_transfer)
            else:
                duplicates += 1
        except Exception:  # noqa: BLE001 - report and continue
            log.exception("Failed to import %s %s", tx.date, tx.amount)
            errors += 1

    return ImportSummary(
        total=len(statement.transactions),
        created=created,
        duplicates=duplicates,
        errors=errors,
        transfers=transfers,
        asset_account_id=asset_id,
        savings_account_ids=savings,
    )


def _earliest_date(statement: BankStatement) -> str | None:
    dates = [tx.date for tx in statement.transactions if tx.date]
    return min(dates) if dates else None


def _own_accounts(
    statement: BankStatement, owner_name: str | None, own_ibans: frozenset[str]
) -> set[str]:
    own = set(own_ibans)
    if owner_name:
        owner_key = _norm_name(owner_name)
        for tx in statement.transactions:
            if (
                tx.counterparty_iban
                and tx.counterparty_iban != statement.account_iban
                and _norm_name(tx.counterparty_name) == owner_key
            ):
                own.add(tx.counterparty_iban)
    own.discard(statement.account_iban)
    return own


def _resolve_opposing_accounts(
    statement: BankStatement,
    writer: StatementWriter,
    savings_ibans: set[str],
    dry_run: bool,
) -> dict[int, str]:
    """Map each non-transfer transaction index to an opposing account id, creating as needed."""
    resolver = AccountResolver()
    if not dry_run:
        for role in ("expense", "revenue"):
            resolver.prime(
                role, [(a.name, a.iban, a.id) for a in writer.list_accounts(role)]
            )

    opposing: dict[int, str] = {}
    for index, tx in enumerate(statement.transactions):
        if tx.counterparty_iban in savings_ibans:
            continue
        role = "expense" if tx.is_outgoing else "revenue"
        account_id = resolver.resolve(tx.counterparty_name, tx.counterparty_iban, role)
        if account_id is None and not dry_run:
            account_id = writer.create_opposing_account(
                tx.counterparty_name, tx.counterparty_iban, role
            )
            resolver.register(tx.counterparty_name, tx.counterparty_iban, account_id, role)
        opposing[index] = account_id or ""
    return opposing
