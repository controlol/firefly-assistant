"""Resolve a counterparty to an existing Firefly opposing account, to avoid duplicates.

Pure/in-memory: the importer primes it with the accounts Firefly already has, then asks it to
resolve each counterparty and registers any newly created account. Expense and revenue are kept
separate because Firefly will not accept an expense account where a revenue one is required.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from rapidfuzz import fuzz, process

# A counterparty whose normalised name scores >= this against a known account is the same account.
FUZZY_THRESHOLD = 90
_LEGAL_SUFFIX = r"\b(b\.?v\.?|n\.?v\.?|gmbh|ltd|inc|s\.?a\.?r\.?l\.?|sa|sca|scs|cv|vof)\b"


def normalise_merchant(name: str) -> str:
    """Canonical merchant key: drop processor prefixes, store numbers, legal forms, punctuation."""
    text = name.lower().strip()
    if "*" in text:  # processor*merchant (e.g. "BCK*Vue Cinemas", "Zettle_*RUIS") -> merchant
        text = text.split("*")[-1]
    text = re.sub(_LEGAL_SUFFIX, " ", text)
    text = re.sub(r"\s+\d{2,}\b", " ", text)  # trailing store / terminal numbers
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class AccountResolver:
    """Roles are "expense" and "revenue" (Firefly keeps them as separate account types)."""

    def __init__(self) -> None:
        self._by_iban: dict[str, dict[str, str]] = {"expense": {}, "revenue": {}}
        self._by_norm: dict[str, dict[str, str]] = {"expense": {}, "revenue": {}}

    def prime(self, role: str, accounts: Iterable[tuple[str, str | None, str]]) -> None:
        for name, iban, account_id in accounts:
            self.register(name, iban, account_id, role)

    def resolve(self, name: str, iban: str | None, role: str) -> str | None:
        """Return an existing account id of this role to reuse, or None to create one."""
        by_iban, by_norm = self._by_iban[role], self._by_norm[role]
        if iban and iban in by_iban:
            return by_iban[iban]
        norm = normalise_merchant(name)
        if not norm:
            return None
        if norm in by_norm:
            return by_norm[norm]
        if by_norm:
            match = process.extractOne(norm, list(by_norm.keys()), scorer=fuzz.token_sort_ratio)
            if match is not None and match[1] >= FUZZY_THRESHOLD:
                return by_norm[match[0]]
        return None

    def register(self, name: str, iban: str | None, account_id: str, role: str) -> None:
        if iban:
            self._by_iban[role].setdefault(iban, account_id)
        norm = normalise_merchant(name)
        if norm:
            self._by_norm[role].setdefault(norm, account_id)
