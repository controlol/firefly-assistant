"""Tests for AccountResolver, incl. the OPT-IN Phase 2.3 embedding last-resort step.

The cascade is IBAN-exact -> normalise_merchant-exact -> rapidfuzz -> (optional) embedding-nearest.
The embedding step only exists when an ``embedder`` is injected; with ``embedder=None`` the resolver
must be byte-identical to the historical IBAN/norm/fuzzy behaviour. Crucially, the embedding step
must NOT merge two clearly different merchants (a false merge corrupts the ledger).

These tests use the network-free ``FakeEmbedder`` for the negative/default-off cases, and a tiny
deterministic ``CollidingEmbedder`` for the positive case so it can force a near-1.0 cosine between
two names that share no tokens (where IBAN/norm/fuzzy would all legitimately miss) — exactly the
"the fake collides them" scenario the embedding step is meant to catch.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from firefly_bot.banking.accounts import AccountResolver
from firefly_bot.enrich import FakeEmbedder


class CollidingEmbedder:
    """A fake that maps a chosen set of aliases to one shared unit vector, everything else apart.

    ``aliases`` are the normalised names that should be treated as the SAME merchant; they all get
    the same one-hot vector (cosine 1.0 with each other). Any other string gets its own unique
    one-hot vector (cosine 0.0 with everything else), so distinct merchants never merge.
    """

    def __init__(self, aliases: set[str], *, dim: int = 64) -> None:
        self._aliases = aliases
        self._dim = dim
        self._index: dict[str, int] = {}
        self._next = 1  # slot 0 is the shared "alias" component

    def _slot(self, text: str) -> int:
        if text in self._aliases:
            return 0
        if text not in self._index:
            self._index[text] = self._next
            self._next += 1
        return self._index[text]

    def _embed(self, texts: list[str]) -> NDArray[np.float32]:
        mat = np.zeros((len(texts), self._dim), dtype=np.float32)
        for row, text in enumerate(texts):
            mat[row, self._slot(text) % self._dim] = 1.0
        return mat

    def embed_passages(self, texts: list[str]) -> NDArray[np.float32]:
        return self._embed(texts)

    def embed_queries(self, texts: list[str]) -> NDArray[np.float32]:
        return self._embed(texts)


# --- default-off: behaviour must be byte-identical to the historical resolver -------------------


def test_iban_exact_match_without_embedder() -> None:
    resolver = AccountResolver()
    resolver.register("Albert Heijn 2264", "NL12ABNA0123456789", "acc-1", "expense")
    # A totally different name but the same IBAN -> same account (IBAN is the strongest signal).
    assert resolver.resolve("Totally Other Name", "NL12ABNA0123456789", "expense") == "acc-1"


def test_norm_and_fuzzy_match_without_embedder() -> None:
    resolver = AccountResolver()
    resolver.register("Albert Heijn 2264", "NL12ABNA0123456789", "acc-1", "expense")
    # normalise_merchant collapses the store number -> norm-exact hit, no IBAN needed.
    assert resolver.resolve("Albert Heijn 2277", None, "expense") == "acc-1"
    # A near-spelling that clears the rapidfuzz token_sort threshold.
    assert resolver.resolve("Albert Heijnn", None, "expense") == "acc-1"


def test_distinct_merchant_creates_new_account_without_embedder() -> None:
    resolver = AccountResolver()
    resolver.register("Albert Heijn", "NL12ABNA0123456789", "acc-1", "expense")
    # A clearly different merchant -> None (the importer will create a fresh account).
    assert resolver.resolve("Shell Station", None, "expense") is None


def test_roles_are_isolated_without_embedder() -> None:
    resolver = AccountResolver()
    resolver.register("Albert Heijn", "NL12ABNA0123456789", "acc-1", "expense")
    # Same name, different role -> must not leak across the expense/revenue boundary.
    assert resolver.resolve("Albert Heijn", None, "revenue") is None


def test_embedder_none_matches_fake_off_behaviour() -> None:
    """An injected embedder that is None must reproduce the no-embedder resolver exactly."""
    plain = AccountResolver()
    with_none = AccountResolver(embedder=None)
    for r in (plain, with_none):
        r.register("Albert Heijn", "NL12ABNA0123456789", "acc-1", "expense")
    assert plain.resolve("Shell", None, "expense") == with_none.resolve("Shell", None, "expense")
    assert (
        plain.resolve("Albert Heijn 99", None, "expense")
        == with_none.resolve("Albert Heijn 99", None, "expense")
        == "acc-1"
    )


# --- positive: the embedding last-resort step DOES reuse an account when it collides -------------


def test_embedding_resolves_colliding_merchant_when_others_miss() -> None:
    # "AH to go" and "Appie" share no tokens with "Albert Heijn" and no IBAN, so IBAN/norm/fuzzy
    # all miss; the colliding embedder treats them as the same merchant, so the embedding step
    # (cosine 1.0 >= gate) reuses the known account instead of creating a duplicate.
    embedder = CollidingEmbedder({"albert heijn", "ah to go", "appie"})
    resolver = AccountResolver(embedder=embedder, embedding_gate=0.93)
    resolver.register("Albert Heijn", "NL12ABNA0123456789", "acc-1", "expense")

    assert resolver.resolve("AH to go", None, "expense") == "acc-1"
    assert resolver.resolve("Appie", None, "expense") == "acc-1"


def test_embedding_only_fires_after_iban_norm_fuzzy() -> None:
    # The colliding aliases also include a token-overlapping name, but IBAN still wins first.
    embedder = CollidingEmbedder({"albert heijn", "appie"})
    resolver = AccountResolver(embedder=embedder, embedding_gate=0.93)
    resolver.register("Albert Heijn", "NL12ABNA0123456789", "acc-1", "expense")
    resolver.register("Jumbo", "NL99RABO0000000001", "acc-2", "expense")
    # Same IBAN as Jumbo -> IBAN-exact wins even though the name collides with the AH alias set.
    assert resolver.resolve("Appie", "NL99RABO0000000001", "expense") == "acc-2"


# --- negative (most important): distinct merchants must NEVER merge ------------------------------


def test_embedding_does_not_merge_distinct_merchants() -> None:
    # "Shell" does not collide with the AH alias set, so the embedder gives it a near-0 cosine to
    # the only known account -> below the gate -> None (a fresh account), no false merge.
    embedder = CollidingEmbedder({"albert heijn", "ah to go"})
    resolver = AccountResolver(embedder=embedder, embedding_gate=0.93)
    resolver.register("Albert Heijn", "NL12ABNA0123456789", "acc-1", "expense")

    assert resolver.resolve("Shell Station", None, "expense") is None
    assert resolver.resolve("KPN Mobiel", None, "expense") is None


def test_embedding_negative_with_real_fake_embedder() -> None:
    # With the real token-based FakeEmbedder, two distinct Dutch merchants share no tokens, so the
    # cosine is 0.0 — far below the 0.93 gate. The high gate is what prevents false merges.
    resolver = AccountResolver(embedder=FakeEmbedder(), embedding_gate=0.93)
    resolver.register("Jumbo Supermarkten", None, "acc-1", "expense")
    assert resolver.resolve("Shell Tankstation", None, "expense") is None
    assert resolver.resolve("KPN", None, "expense") is None


def test_embedding_respects_role_separation() -> None:
    embedder = CollidingEmbedder({"albert heijn", "appie"})
    resolver = AccountResolver(embedder=embedder, embedding_gate=0.93)
    resolver.register("Albert Heijn", None, "acc-1", "expense")
    # The colliding alias is an expense account; a revenue lookup must not reuse it.
    assert resolver.resolve("Appie", None, "revenue") is None
    assert resolver.resolve("Appie", None, "expense") == "acc-1"
