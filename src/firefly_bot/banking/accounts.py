"""Resolve a counterparty to an existing Firefly opposing account, to avoid duplicates.

Pure/in-memory: the importer primes it with the accounts Firefly already has, then asks it to
resolve each counterparty and registers any newly created account. Expense and revenue are kept
separate because Firefly will not accept an expense account where a revenue one is required.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray
from rapidfuzz import fuzz, process

if TYPE_CHECKING:
    from firefly_bot.enrich.embedder import Embedder

# A counterparty whose normalised name scores >= this against a known account is the same account.
FUZZY_THRESHOLD = 90
# Default min cosine for the OPTIONAL embedding last-resort step (see EnrichSettings.merchant_gate).
DEFAULT_EMBEDDING_GATE = 0.93
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
    """Roles are "expense" and "revenue" (Firefly keeps them as separate account types).

    Cascade in :meth:`resolve`: IBAN-exact -> normalise_merchant-exact -> rapidfuzz token_sort.
    When (and only when) an ``embedder`` is injected, a fourth, OPT-IN, LAST-resort embedding step
    runs after those three all miss: it embeds the normalised counterparty name and compares it to
    the embeddings of the already-known account names *for that role*, reusing the nearest account
    only if cosine >= ``embedding_gate``. With ``embedder=None`` (the default) behaviour is
    byte-identical to the IBAN/norm/fuzzy resolver — the embedding path is never touched.

    The gate is high on purpose: wrongly merging two distinct merchants into one account corrupts
    the ledger and is far worse than account proliferation, so this only fires on a near-certain
    match (see docs/ENRICHMENT.md Phase 2.3 and EnrichSettings.merchant_resolution).
    """

    def __init__(
        self,
        *,
        embedder: Embedder | None = None,
        embedding_gate: float = DEFAULT_EMBEDDING_GATE,
    ) -> None:
        self._by_iban: dict[str, dict[str, str]] = {"expense": {}, "revenue": {}}
        self._by_norm: dict[str, dict[str, str]] = {"expense": {}, "revenue": {}}
        self._embedder = embedder
        self._embedding_gate = embedding_gate
        # Per-role cache of known account-name vectors, kept parallel to ``_norm_keys`` so the
        # nearest row maps back to a normalised name (and thence to an account id). Only populated
        # when an embedder is present; rebuilt incrementally in ``register``.
        self._norm_keys: dict[str, list[str]] = {"expense": [], "revenue": []}
        self._norm_vecs: dict[str, NDArray[np.float32]] = {
            "expense": np.zeros((0, 0), dtype=np.float32),
            "revenue": np.zeros((0, 0), dtype=np.float32),
        }

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
        # Last resort, opt-in: embedding-nearest known account name for this role, high gate.
        return self._resolve_by_embedding(norm, role)

    def _resolve_by_embedding(self, norm: str, role: str) -> str | None:
        """Nearest known account name by cosine, reused only if it clears ``embedding_gate``."""
        if self._embedder is None:
            return None
        ref = self._norm_vecs[role]
        if ref.shape[0] == 0:
            return None
        query = self._embedder.embed_queries([norm])[0]
        sims = ref @ query  # cosine: every vector is L2-normalised
        idx = int(np.argmax(sims))
        if float(sims[idx]) < self._embedding_gate:
            return None
        return self._by_norm[role][self._norm_keys[role][idx]]

    def register(self, name: str, iban: str | None, account_id: str, role: str) -> None:
        if iban:
            self._by_iban[role].setdefault(iban, account_id)
        norm = normalise_merchant(name)
        if norm and norm not in self._by_norm[role]:
            self._by_norm[role][norm] = account_id
            self._add_norm_vector(norm, role)

    def _add_norm_vector(self, norm: str, role: str) -> None:
        """Cache the embedding of a newly-known account name (no-op without an embedder)."""
        if self._embedder is None:
            return
        vec = self._embedder.embed_passages([norm])  # (1, dim), L2-normalised
        self._norm_keys[role].append(norm)
        existing = self._norm_vecs[role]
        self._norm_vecs[role] = vec if existing.shape[0] == 0 else np.vstack([existing, vec])
