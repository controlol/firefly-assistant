"""Match an ExtractedInvoice to an existing Firefly transaction.

Signals (additive, capped at 1.0):
  - invoice number found in the transaction reference/description ......... +0.6
  - amount within tolerance .............................................. +0.4
  - counterparty IBAN equals the transaction's source/destination IBAN ... +0.2
  - invoice date close to the transaction date ........................... +0.1 (scaled)

A candidate must hit at least the number or the amount to be considered. **Auto-attach
(>= threshold) effectively requires the invoice number to appear in the transaction** plus one
corroborator — the safest high-precision rule, since the invoice number uniquely ties a
document to its payment while amount/IBAN alone can collide across invoices from one supplier.
Anything positive but below threshold is attached with the `needs-review` tag instead.
"""

from __future__ import annotations

from firefly_bot.config import MatchingSettings
from firefly_bot.models import (
    ExtractedInvoice,
    FireflyTransaction,
    MatchOutcome,
    MatchResult,
)
from firefly_bot.ocr.heuristics import normalise_reference

# Below this length an invoice number is too short to trust as a substring match.
_MIN_REFERENCE_LEN = 5


def match_invoice(
    invoice: ExtractedInvoice,
    transactions: list[FireflyTransaction],
    settings: MatchingSettings,
) -> MatchResult:
    if not invoice.is_actionable:
        return MatchResult(
            invoice=invoice,
            outcome=MatchOutcome.NOT_ACTIONABLE,
            detail="No amount or invoice number could be extracted.",
        )

    candidates = score_candidates(invoice, transactions, settings)
    best: tuple[float, FireflyTransaction] | None = None
    for txn, score, _features in candidates:
        if best is None or score > best[0]:
            best = (score, txn)

    if best is None:
        return MatchResult(
            invoice=invoice,
            outcome=MatchOutcome.NO_MATCH,
            detail="No transaction matched on invoice number or amount.",
        )

    score, txn = best
    outcome = (
        MatchOutcome.AUTO_ATTACHED
        if score >= settings.auto_attach_threshold
        else MatchOutcome.ATTACHED_NEEDS_REVIEW
    )
    return MatchResult(
        invoice=invoice,
        transaction=txn,
        score=round(score, 3),
        outcome=outcome,
        detail=_explain(invoice, txn, settings),
    )


def score_candidates(
    invoice: ExtractedInvoice,
    transactions: list[FireflyTransaction],
    settings: MatchingSettings,
) -> list[tuple[FireflyTransaction, float, dict[str, str | float | bool | None]]]:
    """Score every transaction, returning each positively-scored candidate with its features.

    This is the shared, side-effect-free core of matching: `match_invoice` picks the best of these
    for its decision, and the pipeline logs *all* of them (winner + losers) as `LabelRecord`s so a
    future scorer has the negative examples it needs. The feature dict is the same raw signal the
    additive heuristic consumes, exposed so it can be re-featurised later.
    """
    out: list[tuple[FireflyTransaction, float, dict[str, str | float | bool | None]]] = []
    for txn in transactions:
        score, features = _score(invoice, txn, settings)
        if score > 0:
            out.append((txn, score, features))
    return out


def _number_hit(invoice: ExtractedInvoice, txn: FireflyTransaction) -> bool:
    if not invoice.invoice_number:
        return False
    needle = normalise_reference(invoice.invoice_number)
    if len(needle) < _MIN_REFERENCE_LEN:
        return False
    return needle in normalise_reference(txn.description)


def _score(
    invoice: ExtractedInvoice, txn: FireflyTransaction, settings: MatchingSettings
) -> tuple[float, dict[str, str | float | bool | None]]:
    """Return the additive score and the raw feature dict for one (invoice, txn) pair."""
    number_hit = _number_hit(invoice, txn)
    amount_hit = (
        invoice.total_amount is not None
        and abs(abs(txn.amount) - invoice.total_amount) <= settings.amount_tolerance
    )
    iban_match = bool(
        invoice.counterparty_iban
        and invoice.counterparty_iban in {txn.source_iban, txn.destination_iban}
    )
    amount_delta = (
        float(abs(abs(txn.amount) - invoice.total_amount))
        if invoice.total_amount is not None
        else None
    )
    date_delta = (
        (txn.date - invoice.invoice_date).days if invoice.invoice_date is not None else None
    )
    features: dict[str, str | float | bool | None] = {
        "number_match": number_hit,
        "amount_match": amount_hit,
        "iban_match": iban_match,
        "amount_delta": amount_delta,
        "date_delta": date_delta,
    }

    if not (number_hit or amount_hit):
        return 0.0, features

    score = 0.0
    if number_hit:
        score += 0.6
    if amount_hit:
        score += 0.4
    if iban_match:
        score += 0.2
    if date_delta is not None and abs(date_delta) <= settings.date_window_days:
        score += 0.1 * (1 - abs(date_delta) / settings.date_window_days)
    return min(score, 1.0), features


def _explain(
    invoice: ExtractedInvoice, txn: FireflyTransaction, settings: MatchingSettings
) -> str:
    number_hit = _number_hit(invoice, txn)
    iban_hit = invoice.counterparty_iban in {txn.source_iban, txn.destination_iban}
    return (
        f"number_match={number_hit}; "
        f"amount {invoice.total_amount} ~= {abs(txn.amount)}; "
        f"iban_match={iban_hit}; txn_date={txn.date.isoformat()}"
    )
