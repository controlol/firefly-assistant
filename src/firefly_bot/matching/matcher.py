"""Match an ExtractedInvoice to an existing Firefly transaction.

Scoring (0..1):
  - amount within tolerance is required (a candidate that fails it scores 0).
  - +0.6 base for an in-window amount match.
  - +0.4 if the invoice IBAN equals the transaction's source or destination IBAN.
  - small bonus the closer the dates are.

The best-scoring candidate above `auto_attach_threshold` is auto-attached; a positive but
lower score is attached with the needs-review tag; no candidate is NO_MATCH.
"""

from __future__ import annotations

from firefly_bot.config import MatchingSettings
from firefly_bot.models import (
    ExtractedInvoice,
    FireflyTransaction,
    MatchOutcome,
    MatchResult,
)


def match_invoice(
    invoice: ExtractedInvoice,
    transactions: list[FireflyTransaction],
    settings: MatchingSettings,
) -> MatchResult:
    if not invoice.is_actionable or invoice.total_amount is None:
        return MatchResult(
            invoice=invoice,
            outcome=MatchOutcome.NOT_ACTIONABLE,
            detail="No total amount could be extracted.",
        )

    best: tuple[float, FireflyTransaction] | None = None
    for txn in transactions:
        score = _score(invoice, txn, settings)
        if score > 0 and (best is None or score > best[0]):
            best = (score, txn)

    if best is None:
        return MatchResult(
            invoice=invoice,
            outcome=MatchOutcome.NO_MATCH,
            detail="No transaction matched on amount within the date window.",
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
        detail=_explain(invoice, txn),
    )


def _score(
    invoice: ExtractedInvoice, txn: FireflyTransaction, settings: MatchingSettings
) -> float:
    assert invoice.total_amount is not None
    if abs(abs(txn.amount) - invoice.total_amount) > settings.amount_tolerance:
        return 0.0

    score = 0.6
    if invoice.counterparty_iban and invoice.counterparty_iban in {
        txn.source_iban,
        txn.destination_iban,
    }:
        score += 0.4

    if invoice.invoice_date is not None:
        days = abs((txn.date - invoice.invoice_date).days)
        if days <= settings.date_window_days:
            # Up to +0.1 the closer the dates; never enough to auto-attach on its own.
            score += 0.1 * (1 - days / settings.date_window_days)
    return min(score, 1.0)


def _explain(invoice: ExtractedInvoice, txn: FireflyTransaction) -> str:
    iban_hit = invoice.counterparty_iban in {txn.source_iban, txn.destination_iban}
    return (
        f"amount {invoice.total_amount} ~= {abs(txn.amount)}; "
        f"iban_match={iban_hit}; txn_date={txn.date.isoformat()}"
    )
