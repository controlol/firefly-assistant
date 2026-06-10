"""Phase 1b — correction capture: turn human edits in Firefly into labelled training examples.

Phase 1a emits a `category` `LabelRecord` per imported transaction with ``corrected=None`` — the
bot's *prediction*. When the user later fixes a category in Firefly, that edit is ground truth we
want to learn from. This module reads the *current* Firefly state for the transactions we predicted
(keyed by the ``features["firefly_id"]`` Phase 1a now stores) and, where the live category differs
from what we predicted, appends a NEW record with ``corrected`` set and ``source="user"``.

Append-only: history is never rewritten. Idempotent: a correction already present (same
``firefly_id`` and original ``predicted``) is not written twice. Category only for now — merchant
account correction is deliberately out of scope (see docs/ENRICHMENT.md Phase 1).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from firefly_bot.labels import LabelStore
from firefly_bot.models import FireflyTransaction, LabelRecord


class CorrectionSummary(BaseModel):
    """Counts from one correction-capture pass."""

    model_config = ConfigDict(frozen=True)

    checked: int = 0
    corrections: int = 0
    unchanged: int = 0
    unresolved: int = 0


def _firefly_id(record: LabelRecord) -> str | None:
    value = record.features.get("firefly_id")
    return value if isinstance(value, str) else None


def _existing_correction_keys(prior: Iterable[LabelRecord]) -> set[tuple[str, str | None]]:
    """Keys ``(firefly_id, original-predicted)`` for category corrections already captured.

    A correction is any category record with ``corrected`` set; we key it by the transaction it
    describes and the *original* prediction it corrects, so a re-run recognises it and skips.
    """
    keys: set[tuple[str, str | None]] = set()
    for record in prior:
        if record.kind != "category" or record.corrected is None:
            continue
        fid = _firefly_id(record)
        if fid is not None:
            keys.add((fid, record.predicted))
    return keys


def capture_corrections(
    prior: Iterable[LabelRecord],
    current: Mapping[str, FireflyTransaction],
    store: LabelStore,
) -> CorrectionSummary:
    """Append a correction `LabelRecord` for each prediction the user changed in Firefly.

    ``prior`` is the existing labels.jsonl content; ``current`` maps a Firefly transaction id to its
    *live* state. For each uncorrected ``category`` record with a known ``firefly_id`` found in
    ``current``, compare ``predicted`` to the live ``category_name``. On a difference (including a
    previously-uncategorised txn now categorised, or a changed category) append a new record with
    ``corrected`` = the live category, ``source="user"``, ``score=1.0``. Matches write nothing.
    Records with no ``firefly_id`` or whose transaction is absent from ``current`` are *unresolved*.
    Idempotent: a correction already present in ``prior`` is not re-emitted.
    """
    records = list(prior)
    already = _existing_correction_keys(records)
    ts = datetime.now(tz=UTC)

    checked = corrections = unchanged = unresolved = 0
    for record in records:
        if record.kind != "category" or record.corrected is not None:
            continue
        fid = _firefly_id(record)
        if fid is None or fid not in current:
            unresolved += 1
            continue
        checked += 1
        truth = current[fid].category_name
        # Skip when unchanged, and when the category was *cleared* (truth is None): a cleared
        # category is an unreliable signal and, more importantly, recording `corrected=None` would
        # collide with the "uncorrected prediction" sentinel and re-emit forever. So a captured
        # correction always has a non-None `corrected`.
        if truth is None or truth == record.predicted:
            unchanged += 1
            continue
        if (fid, record.predicted) in already:
            continue  # correction already captured on a prior run
        already.add((fid, record.predicted))
        store.record(
            LabelRecord(
                ts=ts,
                kind="category",
                features=dict(record.features),
                predicted=record.predicted,
                score=1.0,
                corrected=truth,
                source="user",
            )
        )
        corrections += 1

    return CorrectionSummary(
        checked=checked,
        corrections=corrections,
        unchanged=unchanged,
        unresolved=unresolved,
    )
