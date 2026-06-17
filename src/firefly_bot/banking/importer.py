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
from firefly_bot.enrich.embedder import Embedder
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
        create: bool = True,
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
    merchant_embedder: Embedder | None = None,
    merchant_gate: float = 0.93,
    create_account: bool = True,
) -> ImportSummary:
    # Default to a no-op store on dry-run (mirrors the ledger), otherwise accumulate signal.
    store: LabelStore = label_store or NullLabelStore()
    own = _own_accounts(statement, owner_name, own_ibans)

    # create_account=False makes a missing asset account a hard error (AssetAccountNotFoundError)
    # instead of silently creating a duplicate; the IBAN must already exist on a Firefly account.
    asset_id = "" if dry_run else writer.ensure_asset_account(
        statement.account_iban, statement.currency, "defaultAsset",
        f"{account_name} {statement.account_iban[-6:]}",
        opening_balance=statement.opening_balance,
        opening_date=statement.opening_date or _earliest_date(statement),
        create=create_account,
    )
    savings = {} if dry_run else {
        iban: writer.ensure_asset_account(
            iban, statement.currency, "savingAsset", f"Spaarrekening {iban[-6:]}",
            create=create_account,
        )
        for iban in sorted(own)
    }

    # Existing opposing accounts are fetched once and shared by the resolver and the collector-IBAN
    # detector (avoids a second round-trip). Skipped on dry-run, which does no Firefly I/O.
    existing_accounts: dict[str, list[FireflyAccount]] = (
        {}
        if dry_run
        else {role: writer.list_accounts(role) for role in ("expense", "revenue")}
    )
    collector_ibans = _collector_ibans(statement, existing_accounts)
    opposing = _resolve_opposing_accounts(
        statement, writer, set(savings), dry_run, existing_accounts,
        merchant_embedder=merchant_embedder, merchant_gate=merchant_gate,
    )

    created = duplicates = errors = transfers = 0
    for index, tx in enumerate(statement.transactions):
        is_transfer = tx.counterparty_iban in savings
        # A collector IBAN (Adyen/Mollie/payment-request services) resolves to ONE shared expense
        # account, so the per-transaction merchant would be lost. Keep the shared account but record
        # the real merchant in the description (see _with_merchant).
        description = tx.description
        if not is_transfer and tx.counterparty_iban in collector_ibans:
            description = _with_merchant(tx.counterparty_name, tx.description)
        split: dict[str, object] = {
            "date": tx.date,
            "amount": str(tx.amount),
            "description": description,
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


def _collector_ibans(
    statement: BankStatement, existing_accounts: dict[str, list[FireflyAccount]]
) -> set[str]:
    """IBANs that map to >1 distinct merchant name — payment-processor collector accounts.

    Built from the statement's own counterparties plus the opposing accounts already in Firefly, so
    an IBAN polluted by an earlier single-merchant import is still recognised. Such an IBAN is not a
    single merchant's identity (Adyen/Mollie/Betaalverzoek settle many merchants through one IBAN);
    rather than merge them into one account silently, the importer keeps the shared account but
    records each real merchant in the transaction description.
    """
    names_by_iban: dict[str, set[str]] = {}

    def add(iban: str | None, name: str) -> None:
        if not iban:
            return
        norm = normalise_merchant(name)
        if norm:
            names_by_iban.setdefault(iban, set()).add(norm)

    for tx in statement.transactions:
        add(tx.counterparty_iban, tx.counterparty_name)
    for accounts in existing_accounts.values():
        for account in accounts:
            add(account.iban, account.name)
    return {iban for iban, names in names_by_iban.items() if len(names) > 1}


def _with_merchant(name: str, description: str) -> str:
    """Prefix the description with the real merchant, for shared/collector-IBAN transactions.

    Idempotent and deterministic (so re-imports hash identically and still dedup): a no-op when the
    merchant text is already present or empty. Result is clamped to Firefly's 255-char limit.
    """
    name = name.strip()
    if not name or name.lower() in description.lower():
        return description
    return f"{name} — {description}".strip(" —")[:255]


def _resolve_opposing_accounts(
    statement: BankStatement,
    writer: StatementWriter,
    savings_ibans: set[str],
    dry_run: bool,
    existing_accounts: dict[str, list[FireflyAccount]],
    *,
    merchant_embedder: Embedder | None = None,
    merchant_gate: float = 0.93,
) -> dict[int, str]:
    """Map each non-transfer transaction index to an opposing account id, creating as needed.

    With ``merchant_embedder`` set (Phase 2.3, opt-in), the resolver gets an embedding last-resort
    step after IBAN/norm/fuzzy miss; without it (the default), resolution is the historical
    IBAN/norm/fuzzy cascade. ``existing_accounts`` is empty on dry-run (no priming, no Firefly I/O).
    """
    resolver = AccountResolver(embedder=merchant_embedder, embedding_gate=merchant_gate)
    for role, accounts in existing_accounts.items():
        resolver.prime(role, [(a.name, a.iban, a.id) for a in accounts])

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
