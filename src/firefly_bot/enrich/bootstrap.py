"""Bootstrap importer (cold-start booster) — docs/COLD_START.md item #1, docs/ENRICHMENT.md Phase 2.

A one-off, READ-ONLY pass over the user's *existing* Firefly history that seeds
``./data/labels.jsonl`` with their already-categorised transactions, so the Phase 2 k-NN enricher
is warm on the first real run. This is the single highest-leverage cold-start input: it converts a
"cold" dataset into a "lukewarm" one instantly, using ground truth the user already produced.

For each transaction that already has a category we emit:

- a ``category`` `LabelRecord` (``predicted`` = the existing category) — the trusted seed. Because
  this is confirmed human ground truth, ``source="user"`` and ``score=1.0`` (not an auto guess).
- a ``merchant`` `LabelRecord` (``predicted`` = the counterparty name) when a counterparty name is
  known, so merchant entity resolution (Phase 2.3) has a head start too.

Transactions without a category are skipped — there is nothing to learn from them yet.

**Idempotency.** Re-running bootstrap must never duplicate seeds. We key each record by
``(kind, normalised-embedded-text, normalised-predicted)`` and skip any key already present in the
``existing`` labels (read from labels.jsonl) or emitted earlier in the same run.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from firefly_bot.enrich.categoriser import example_text
from firefly_bot.labels import LabelStore
from firefly_bot.models import FireflyTransaction, LabelRecord


class BootstrapSummary(BaseModel):
    """Counts from one bootstrap pass over existing Firefly history."""

    model_config = ConfigDict(frozen=True)

    scanned: int = 0
    categorised: int = 0
    category_records: int = 0
    merchant_records: int = 0
    skipped_duplicates: int = 0


def _norm(text: str | None) -> str:
    """Normalise for idempotency keying: lowercase + collapse/trim whitespace."""
    return " ".join((text or "").lower().split())


def _key(record: LabelRecord) -> tuple[str, str, str]:
    """Idempotency key: (kind, normalised embedded text, normalised predicted label).

    The embedded text mirrors what the categoriser embeds — "{counterparty_name} {description}" for
    a category record, and the counterparty name for a merchant record — so a record reconstructed
    from labels.jsonl keys identically to a freshly emitted one.
    """
    name = record.features.get("counterparty_name")
    name_str = name if isinstance(name, str) else ""
    if record.kind == "category":
        description = record.features.get("description")
        desc_str = description if isinstance(description, str) else ""
        text = example_text(name_str, desc_str)
    else:
        text = name_str
    return (record.kind, _norm(text), _norm(record.predicted))


def bootstrap_labels(
    transactions: Iterable[FireflyTransaction],
    store: LabelStore,
    *,
    existing: Iterable[LabelRecord] = (),
) -> BootstrapSummary:
    """Seed ``store`` from already-categorised Firefly transactions (read-only on Firefly).

    Returns a `BootstrapSummary`. ``existing`` is the prior labels.jsonl content, used for
    idempotency so re-running never duplicates seeds.
    """
    seen: set[tuple[str, str, str]] = {_key(rec) for rec in existing}
    ts = datetime.now(tz=UTC)

    scanned = 0
    categorised = 0
    category_records = 0
    merchant_records = 0
    skipped_duplicates = 0

    for tx in transactions:
        scanned += 1
        category = tx.category_name
        if not category:
            continue  # nothing to learn yet — skip uncategorised transactions
        categorised += 1
        name = tx.counterparty_name

        category_record = LabelRecord(
            ts=ts,
            kind="category",
            features={
                "counterparty_name": name,
                "description": tx.description,
            },
            predicted=category,
            score=1.0,
            source="user",  # confirmed ground truth, the trusted seed
        )
        if _key(category_record) in seen:
            skipped_duplicates += 1
        else:
            seen.add(_key(category_record))
            store.record(category_record)
            category_records += 1

        if name:
            merchant_record = LabelRecord(
                ts=ts,
                kind="merchant",
                features={
                    "counterparty_name": name,
                    "counterparty_iban": tx.counterparty_iban,
                },
                predicted=name,
                score=1.0,
                source="user",
            )
            if _key(merchant_record) in seen:
                skipped_duplicates += 1
            else:
                seen.add(_key(merchant_record))
                store.record(merchant_record)
                merchant_records += 1

    return BootstrapSummary(
        scanned=scanned,
        categorised=categorised,
        category_records=category_records,
        merchant_records=merchant_records,
        skipped_duplicates=skipped_duplicates,
    )
