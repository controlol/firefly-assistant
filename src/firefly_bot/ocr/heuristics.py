"""Deterministic extraction of total amount and counterparty IBAN from Dutch invoices.

These need no model — a Dutch IBAN matches a tight regex (essentially 100% reliable) and the
total is found by anchoring on Dutch total keywords. Kept pure and side-effect free so it is
trivially unit-testable (see tests/test_heuristics.py).
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from firefly_bot.models import FieldConfidence

# Dutch IBANs: NL + 2 check digits + 4-letter bank code + 10 digits. Allow spaces in source.
_IBAN_RE = re.compile(r"\bNL\d{2}\s?(?:[A-Z]{4})\s?(?:\d{4}\s?){2}\d{2}\b", re.IGNORECASE)

# Keyword detection runs against a "despaced" line (spaces, dots and colons removed) because
# OCR frequently glues a label to its amount, e.g. "Totaalincl.BTW1.562,50".
#
# STRONG keywords name the amount actually due (HIGH confidence). WEAK keywords (plain
# "totaal") are ambiguous (MEDIUM). NEGATIVE markers identify pre-VAT subtotals and per-line
# VAT rows that must never be taken as the grand total.
_STRONG_KEYWORDS: tuple[str, ...] = (
    "tebetalen",
    "totaalinclusiefbtw",
    "totaalinclbtw",
    "totaalincl",
    "totaalteveldoen",
)
_WEAK_KEYWORDS: tuple[str, ...] = ("totaalbedrag", "totaal", "total")
_NEGATIVE_KEYWORDS: tuple[str, ...] = (
    "exclusief",
    "exclbtw",
    "totaalexcl",
    "subtotaal",
    "btwover",
    "btw21",
    "btw9",
)

# A money amount in NL formatting: 1.234,56 or 1234,56 or 1234.56, optional € prefix.
_AMOUNT_RE = re.compile(
    r"(?:€\s?)?(?P<amount>\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})|\d+(?:[.,]\d{2}))"
)

# How many following lines to scan for an amount when the keyword line itself has none.
_LOOKAHEAD_LINES = 2


def extract_iban(text: str) -> tuple[str | None, FieldConfidence]:
    """Return the first Dutch IBAN found, normalised (no spaces, upper-case)."""
    match = _IBAN_RE.search(text)
    if match is None:
        return None, FieldConfidence.NONE
    iban = re.sub(r"\s+", "", match.group(0)).upper()
    return iban, FieldConfidence.HIGH


def _parse_nl_amount(raw: str) -> Decimal | None:
    """Parse a Dutch-formatted money string into a Decimal."""
    cleaned = raw.replace("€", "").replace(" ", "").strip()
    if "," in cleaned:
        # Comma is the decimal separator; dots are thousands separators.
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _despace(line: str) -> str:
    """Lower-case and strip spaces/dots/colons so glued OCR labels still match keywords."""
    return re.sub(r"[\s.:]", "", line).lower()


def extract_total(text: str) -> tuple[Decimal | None, FieldConfidence]:
    """Find the amount actually due.

    1. STRONG keyword (despaced) on a non-negative line -> HIGH. The amount may be on the same
       line (possibly glued to the label) or on one of the next few lines.
    2. `excl + BTW = incl` reconciliation: if a subtotal and a VAT amount sum to an amount that
       appears in the document, that sum is the grand total -> HIGH.
    3. WEAK keyword -> MEDIUM.
    4. Largest amount anywhere -> LOW.
    """
    lines = text.splitlines()
    keys = [_despace(ln) for ln in lines]

    strong = _keyword_amount(lines, keys, _STRONG_KEYWORDS)
    if strong is not None:
        return strong, FieldConfidence.HIGH

    reconciled = _reconcile_excl_plus_vat(lines, keys)
    if reconciled is not None:
        return reconciled, FieldConfidence.HIGH

    weak = _keyword_amount(lines, keys, _WEAK_KEYWORDS)
    if weak is not None:
        return weak, FieldConfidence.MEDIUM

    amounts = [amt for ln in lines if (amt := _largest_amount_in(ln)) is not None]
    if amounts:
        return max(amounts), FieldConfidence.LOW
    return None, FieldConfidence.NONE


def _keyword_amount(
    lines: list[str], keys: list[str], keywords: tuple[str, ...]
) -> Decimal | None:
    """Largest amount on (or just after) the last line matching any keyword, skipping negatives.

    The last match is preferred because the grand total typically sits below subtotals.
    """
    best: Decimal | None = None
    for idx, key in enumerate(keys):
        if not key or any(neg in key for neg in _NEGATIVE_KEYWORDS):
            continue
        if not any(kw in key for kw in keywords):
            continue
        amount = _largest_amount_in(lines[idx])
        if amount is None:
            amount = _amount_in_following(lines, idx)
        if amount is not None:
            best = amount  # later wins
    return best


def _amount_in_following(lines: list[str], idx: int) -> Decimal | None:
    """Scan the next few non-empty lines for an amount (label/value split across lines)."""
    for offset in range(1, _LOOKAHEAD_LINES + 1):
        nxt = idx + offset
        if nxt >= len(lines):
            break
        amount = _largest_amount_in(lines[nxt])
        if amount is not None:
            return amount
    return None


def _reconcile_excl_plus_vat(lines: list[str], keys: list[str]) -> Decimal | None:
    """If a pre-VAT subtotal plus a VAT amount equals an amount present, that's the grand total.

    This nails invoices whose grand total carries no label of its own, using the fact that
    incl = excl + BTW on every Dutch invoice.
    """
    all_amounts: set[Decimal] = {
        amt for ln in lines for amt in _amounts_in(ln)
    }
    excls = _amounts_near(lines, keys, ("exclusief", "subtotaal", "totaalexcl"))
    vats = _amounts_near(lines, keys, ("btwover", "btw21", "btw9", "btw"))
    for excl in excls:
        for vat in vats:
            candidate = excl + vat
            if candidate > excl and candidate in all_amounts:
                return candidate
    return None


def _amounts_near(
    lines: list[str], keys: list[str], markers: tuple[str, ...]
) -> list[Decimal]:
    """Amounts on or just after any line whose despaced key contains a marker."""
    found: list[Decimal] = []
    for idx, key in enumerate(keys):
        if key and any(m in key for m in markers):
            found.extend(_amounts_in(lines[idx]))
            following = _amount_in_following(lines, idx)
            if following is not None:
                found.append(following)
    return found


def _amounts_in(line: str) -> list[Decimal]:
    return [
        amt
        for m in _AMOUNT_RE.finditer(line)
        if (amt := _parse_nl_amount(m.group("amount"))) is not None
    ]


def _largest_amount_in(line: str) -> Decimal | None:
    candidates = _amounts_in(line)
    return max(candidates) if candidates else None
