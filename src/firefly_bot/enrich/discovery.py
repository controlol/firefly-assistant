"""New-label discovery + active-learning review order (docs/COLD_START.md step 4 + Phase 2.2).

When the categoriser abstains (provenance ``none``) or routes a weak match to review, the
transaction is an *orphan*: the bot has no category for it yet. A single orphan is noise, but a
*recurring cluster* of similar orphans is signal — the data is telling us a category is MISSING.
This module turns the orphan pile into candidate new categories and orders human review by impact.

Two pure-numpy functions, no new third-party deps:

- ``discover_labels`` embeds the orphan texts, greedily single-link clusters them by cosine
  similarity, drops clusters too small to be evidence, names each from its dominant merchant /
  shared token, and returns them sorted by size (recurrence) descending.
- ``review_order`` ranks needs-review transactions by an active-learning priority — uncertainty
  (1 - confidence) weighted by frequency (near-duplicate count) — so the few items reviewed first
  settle the most transactions.
"""

from __future__ import annotations

from collections import Counter

import numpy as np
from numpy.typing import NDArray

from firefly_bot.banking.accounts import normalise_merchant
from firefly_bot.enrich.embedder import Embedder
from firefly_bot.models import CandidateLabel

# Stop-words that are merchant-agnostic bank/transaction mechanics: they recur across unrelated
# orphans, so they make terrible category names. Dropped before picking the dominant token.
_NAME_STOPWORDS = frozenset(
    {
        "betaling",
        "pinbetaling",
        "incasso",
        "machtiging",
        "afschrijving",
        "transactie",
        "via",
        "ref",
        "nr",
        "de",
        "het",
        "een",
        "en",
        "van",
        "voor",
        "aan",
    }
)


def _cluster_indices(sims: NDArray[np.float32], threshold: float) -> list[list[int]]:
    """Greedy single-link (connected-components) clustering over a cosine-similarity matrix.

    Two texts are linked when their cosine >= ``threshold``; a cluster is a connected component of
    that graph. Pure numpy + an iterative flood fill (no recursion, no scikit-learn). Order is
    deterministic: components are grown from the lowest unvisited index outward.
    """
    n = sims.shape[0]
    adjacency = sims >= threshold
    visited = [False] * n
    clusters: list[list[int]] = []
    for start in range(n):
        if visited[start]:
            continue
        component: list[int] = []
        stack = [start]
        visited[start] = True
        while stack:
            node = stack.pop()
            component.append(node)
            for neighbour in range(n):
                if not visited[neighbour] and bool(adjacency[node, neighbour]):
                    visited[neighbour] = True
                    stack.append(neighbour)
        clusters.append(sorted(component))
    return clusters


def _cohesion(vecs: NDArray[np.float32], members: list[int]) -> float:
    """Mean pairwise cosine within a cluster (vectors are L2-normalised -> dot == cosine).

    A singleton is perfectly cohesive by definition (1.0). The mean excludes the self-pairs on the
    diagonal so it reflects only the *between-member* tightness, and is clamped to [0, 1].
    """
    if len(members) < 2:
        return 1.0
    sub = vecs[members]
    sims = sub @ sub.T
    n = len(members)
    off_diagonal_sum = float(sims.sum() - np.trace(sims))
    mean = off_diagonal_sum / (n * (n - 1))
    # Round away float32 dot-product noise (identical vectors give 0.9999998, not 1.0) and clamp.
    return max(0.0, min(1.0, round(mean, 6)))


def _suggest_name(texts: list[str]) -> str:
    """Name a cluster from the most common normalised-merchant token across its members.

    Reuses ``normalise_merchant`` (the same canonicalisation the account resolver uses) to strip
    processor prefixes, store numbers, and legal forms, then picks the most frequent meaningful
    token. Stop-words (bank mechanics) are dropped first; if every token is a stop-word we fall
    back to the most frequent shared alphanumeric token so we always return *something*.
    """
    meaningful: Counter[str] = Counter()
    fallback: Counter[str] = Counter()
    for text in texts:
        tokens = normalise_merchant(text).split()
        seen = set()
        for token in tokens:
            if token in seen:
                continue  # count each token once per member -> "shared across members"
            seen.add(token)
            fallback[token] += 1
            if token not in _NAME_STOPWORDS and not token.isdigit():
                meaningful[token] += 1
    pool = meaningful or fallback
    if not pool:
        return "Onbekend"
    # Most frequent token wins; on a tie prefer the *longer* token (more descriptive — e.g.
    # "parkeergarage" over the "park" fragment of "Q-Park"), then alphabetical for determinism.
    best = min(pool.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))[0]
    return best.capitalize()


def discover_labels(
    texts: list[str],
    embedder: Embedder,
    *,
    min_size: int = 3,
    threshold: float = 0.90,
) -> list[CandidateLabel]:
    """Cluster orphan texts into candidate new categories (docs/COLD_START.md step 4).

    Args:
        texts: the orphan transaction texts (``"{counterparty} {description}"``), as embedded
            elsewhere as *passages*.
        embedder: the injected embedding seam (real e5 or the test fake).
        min_size: minimum cluster size to surface a candidate. Smaller clusters are dropped —
            a one-off orphan is not yet enough evidence that a category is missing.
        threshold: cosine link threshold for single-link clustering. Default 0.90 is the
            production-correct value for the real e5 embedder: e5's high similarity floor + small
            inter-class margins mean real Dutch data separates into distinct themes (civic fees vs
            parking) only around ~0.90 — single-link *chains* everything into one blob below that.
            It sits well above the categoriser's ``gate`` (0.83) because forming a *new* label
            demands genuine near-duplicates, a higher bar than merely matching an existing label.
            The unit tests pass lower explicit thresholds tuned to the deterministic FakeEmbedder.

    Returns:
        Candidate labels with size >= ``min_size``, sorted by size descending (most recurring /
        highest impact first), ties broken by cohesion descending then name for determinism.
    """
    if not texts:
        return []
    vecs = embedder.embed_passages(texts)
    sims = vecs @ vecs.T
    candidates: list[CandidateLabel] = []
    for members in _cluster_indices(sims, threshold):
        if len(members) < min_size:
            continue  # not enough evidence yet — a singleton/small cluster is dropped
        member_texts = [texts[i] for i in members]
        candidates.append(
            CandidateLabel(
                suggested_name=_suggest_name(member_texts),
                member_texts=member_texts,
                size=len(members),
                cohesion=_cohesion(vecs, members),
            )
        )
    candidates.sort(key=lambda c: (-c.size, -c.cohesion, c.suggested_name))
    return candidates


def review_order(items: list[tuple[str, float]]) -> list[int]:
    """Order needs-review transactions by active-learning priority (impact x uncertainty).

    Each item is a ``(text, confidence)`` pair. Priority = uncertainty ``(1 - confidence)``
    weighted by frequency (how many items share the same normalised text). Reviewing a frequent,
    uncertain item settles a whole group at once, so it goes first; a confident one-off goes last.

    Returns indices into ``items`` ordered most-impactful-and-uncertain first. Deterministic: ties
    break by original index, so equal-priority items keep their input order.
    """
    counts: Counter[str] = Counter(normalise_merchant(text) for text, _ in items)
    scored: list[tuple[float, int]] = []
    for index, (text, confidence) in enumerate(items):
        uncertainty = 1.0 - confidence
        frequency = counts[normalise_merchant(text)]
        priority = uncertainty * frequency
        scored.append((priority, index))
    scored.sort(key=lambda pi: (-pi[0], pi[1]))
    return [index for _, index in scored]
