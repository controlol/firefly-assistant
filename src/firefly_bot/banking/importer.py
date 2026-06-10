"""Import a parsed CAMT statement into Firefly III.

Depends only on the `StatementWriter` protocol, so it runs against a fake in tests. Idempotency
is delegated to Firefly's duplicate-hash detection (skip_duplicates), so re-importing the same
or overlapping statements does not create duplicate transactions.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel

from firefly_bot.banking.accounts import AccountResolver, normalise_merchant
from firefly_bot.banking.camt import BankStatement, BankTransaction, reconciles
from firefly_bot.banking.mcc import category_for_mcc
from firefly_bot.enrich.categoriser import Categoriser
from firefly_bot.labels import LabelStore, NullLabelStore
from firefly_bot.models import FireflyAccount, LabelRecord

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
    def create_transaction(
        self, split: dict[str, object], *, skip_duplicates: bool
    ) -> str | None: ...


class ImportSummary(BaseModel):
    total: int
    created: int
    duplicates: int
    errors: int
    transfers: int
    asset_account_id: str
    savings_account_ids: dict[str, str]
    # Whether the statement's opening + entries == closing. None when balances are absent.
    reconciled: bool | None = None


def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _capture_tx_labels(
    store: LabelStore,
    tx: BankTransaction,
    category: str | None,
    opposing_id: str | None,
    *,
    category_score: float = 1.0,
    provenance: str = "mcc",
    firefly_id: str | None = None,
) -> None:
    """Emit one `category` and one `merchant` LabelRecord for a transaction (Phase 1 capture).

    `category.predicted` is the categoriser's chosen label (None when undetermined / routed to
    review); `category.score` is its confidence and `features["provenance"]` records *why* it was
    chosen (`mcc` | `knn` | `zeroshot` | `none`), so a future scorer can weight sources differently.
    `merchant.predicted` is the resolved/created opposing account id. Both log the raw inputs so a
    future enricher can re-featurise. The merchant `score` stays 1.0 — the IBAN/fuzzy resolver is a
    deterministic, high-precision decision, not a probabilistic one.

    `firefly_id` is the id of the transaction Firefly just created (None when it was a duplicate or
    on dry-run); both records store it in `features["firefly_id"]` so Phase 1b can key a later human
    correction in Firefly back to the original prediction.
    """
    ts = datetime.now(tz=UTC)
    store.record(
        LabelRecord(
            ts=ts,
            kind="category",
            features={
                "mcc": tx.mcc,
                "counterparty_name": tx.counterparty_name,
                "description": tx.description,
                "provenance": provenance,
                "firefly_id": firefly_id,
            },
            predicted=category,
            score=category_score,
            source="auto",
        )
    )
    store.record(
        LabelRecord(
            ts=ts,
            kind="merchant",
            features={
                "counterparty_name": tx.counterparty_name,
                "counterparty_iban": tx.counterparty_iban,
                "merchant_key": normalise_merchant(tx.counterparty_name),
                "firefly_id": firefly_id,
            },
            predicted=opposing_id,
            score=1.0,
            source="auto",
        )
    )


def _categorise(
    tx: BankTransaction,
    is_transfer: bool,
    categoriser: Categoriser | None,
    split: dict[str, object],
    needs_review_tag: str,
) -> tuple[str | None, float, str]:
    """Decide a transaction's category and return (label, confidence, provenance).

    Transfers stay uncategorised. With no categoriser this is the historical MCC-only behaviour
    (provenance ``mcc``, confidence 1.0). With a categoriser, the full cascade runs: confident
    suggestions (``categoriser.is_auto``) set the category; weak/none ones leave it unset and get
    the ``needs_review_tag`` appended — the bot never auto-writes a guess it isn't sure of.
    """
    if is_transfer:
        return None, 1.0, "mcc"
    if categoriser is None:
        return category_for_mcc(tx.mcc), 1.0, "mcc"

    suggestion = categoriser.suggest(tx.counterparty_name, tx.description, tx.mcc)
    if categoriser.is_auto(suggestion) and suggestion.label:
        return suggestion.label, suggestion.confidence, suggestion.provenance
    # Weak / none: do not set a category; flag for human review.
    tags = split.setdefault("tags", [])
    if isinstance(tags, list) and needs_review_tag not in tags:
        tags.append(needs_review_tag)
    return None, suggestion.confidence, suggestion.provenance


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
    label_store: LabelStore | None = None,
    categoriser: Categoriser | None = None,
    needs_review_tag: str = "needs-review",
) -> ImportSummary:
    # Default to a no-op store on dry-run (mirrors the ledger), otherwise accumulate signal.
    store: LabelStore = label_store or NullLabelStore()
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
        category, score, provenance = _categorise(
            tx, is_transfer, categoriser, split, needs_review_tag
        )
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
            _capture_tx_labels(
                store, tx, category, opposing.get(index),
                category_score=score, provenance=provenance,
            )
            created += 1
            transfers += int(is_transfer)
            continue
        firefly_id: str | None = None
        try:
            firefly_id = writer.create_transaction(split, skip_duplicates=skip_duplicates)
            if firefly_id is not None:
                created += 1
                transfers += int(is_transfer)
            else:
                duplicates += 1
        except Exception:  # noqa: BLE001 - report and continue
            log.exception("Failed to import %s %s", tx.date, tx.amount)
            errors += 1
        _capture_tx_labels(
            store, tx, category, opposing.get(index),
            category_score=score, provenance=provenance, firefly_id=firefly_id,
        )

    return ImportSummary(
        total=len(statement.transactions),
        created=created,
        duplicates=duplicates,
        errors=errors,
        transfers=transfers,
        asset_account_id=asset_id,
        savings_account_ids=savings,
        reconciled=reconciles(statement),
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
