"""Embedding categoriser: the Phase 2 cascade (docs/COLD_START.md steps 1 + 3).

Given a transaction's counterparty, description, and optional MCC, propose a category with its
provenance and confidence:

    1. MCC      — deterministic MCC->category map (no ML, confidence 1.0).
    2/3. embed once, then resolve k-NN (nearest labelled *example*) and zero-shot (nearest label
         *name*) with a two-threshold rule tuned for cold start:
         a. a *confident* k-NN (sim >= ``knn_trust``) wins — a strong example match is the best
            signal we have, and it carries merchant-specific knowledge a bare label name can't.
         b. otherwise a gated zero-shot label-name match (sim >= ``gate``) wins — curated names are
            robust even with zero examples, and don't suffer the tiny-example-set noise below.
         c. otherwise a *weak* k-NN (sim >= ``gate``) is taken as a last resort before abstaining.
    4. none     — nothing cleared the gate; the caller should mark the txn needs-review.

Why not simply "take the stronger cosine" (the first cut, per an earlier reading of
docs/COLD_START.md step 3)? Because k-NN-vs-example and zero-shot-vs-name similarities are *not*
on the same scale: with only a handful of examples e5's high similarity floor makes a spurious
nearest example (e.g. Eneco->Brandstof at ~0.84) outrank the correct label *name* (Energie). So a
weak k-NN must not overrule a curated label name — only a confident one (``knn_trust``) may. As
examples accumulate (the density loop) confident k-NN fires more often and this naturally relaxes.

The gate is an *absolute* top-similarity threshold (~0.83), not an inter-category margin: the
embeddings spike showed e5 gives high baseline similarities with small inter-class margins.

All example and label-name vectors are embedded once at construction and cached as numpy
matrices; similarity is a dot product because every vector is L2-normalised.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
from numpy.typing import NDArray

from firefly_bot.banking.mcc import MCC_CATEGORIES, category_for_mcc
from firefly_bot.enrich.embedder import Embedder
from firefly_bot.models import CategorySuggestion, LabelRecord


def _feature_str(features: dict[str, str | float | bool | None], key: str) -> str:
    value = features.get(key)
    return str(value) if isinstance(value, str) else ""


def example_text(counterparty_name: str, description: str) -> str:
    """The canonical text we embed for a transaction (and for a labelled example)."""
    return f"{counterparty_name} {description}".strip()


class Categoriser:
    """Suggest a category for a transaction via MCC -> k-NN -> zero-shot, gated on absolute sim.

    Built from labelled *examples* (``category``-kind `LabelRecord`s that already have a label)
    and a label *inventory* (the set of category names = labels seen in examples plus every
    category the MCC map can emit). Example and label-name embeddings are precomputed once.
    """

    def __init__(
        self,
        examples: Iterable[LabelRecord],
        label_inventory: Iterable[str],
        embedder: Embedder,
        *,
        gate: float = 0.83,
        knn_trust: float = 0.90,
    ) -> None:
        self._embedder = embedder
        self._gate = gate
        self._knn_trust = knn_trust

        # --- labelled examples (k-NN reference set) ------------------------------------------
        self._example_texts: list[str] = []
        self._example_labels: list[str] = []
        for rec in examples:
            label = rec.predicted
            if rec.kind != "category" or label is None:
                continue  # only labelled category examples anchor the k-NN lookup
            text = example_text(
                _feature_str(rec.features, "counterparty_name"),
                _feature_str(rec.features, "description"),
            )
            if not text:
                continue
            self._example_texts.append(text)
            self._example_labels.append(label)

        # --- label inventory (zero-shot reference set) ---------------------------------------
        # Names present in examples PLUS every category the MCC map can emit, deduped, ordered.
        names: dict[str, None] = {}
        for name in (*self._example_labels, *MCC_CATEGORIES.values(), *label_inventory):
            if name:
                names.setdefault(name, None)
        self._label_names: list[str] = list(names)

        # --- precomputed, cached embedding matrices ------------------------------------------
        self._example_vecs: NDArray[np.float32] = embedder.embed_passages(self._example_texts)
        self._label_name_vecs: NDArray[np.float32] = embedder.embed_passages(self._label_names)

    def is_auto(self, s: CategorySuggestion) -> bool:
        """The shared confidence policy: is this suggestion strong enough to auto-apply?

        Mirrors the write-path rule the demo (``experiments/enrich_demo.py``) computes inline, so
        the importer and the demo agree: deterministic MCC is always trusted; a *confident* k-NN
        (>= ``knn_trust``) carries merchant-specific knowledge worth writing; a gated zero-shot
        label name (>= ``gate``) is robust at cold start. Everything else (weak k-NN, ``none``) is
        only a suggestion and is routed to needs-review — the bot never auto-writes an unsure guess.
        """
        if s.provenance == "mcc":
            return True
        if s.provenance == "knn":
            return s.confidence >= self._knn_trust
        if s.provenance == "zeroshot":
            return s.confidence >= self._gate
        return False

    def suggest(
        self, counterparty_name: str, description: str, mcc: str | None
    ) -> CategorySuggestion:
        """Run the cascade for one transaction (docs/COLD_START.md steps 1 + 3)."""
        # 1. Deterministic MCC -> category. Highest precision, zero ML, confidence 1.0.
        mcc_category = category_for_mcc(mcc)
        if mcc_category is not None:
            return CategorySuggestion(
                label=mcc_category, confidence=1.0, provenance="mcc", evidence=mcc
            )

        query = example_text(counterparty_name, description)
        if not query:
            return CategorySuggestion(label=None, confidence=0.0, provenance="none")
        query_vec = self._embedder.embed_queries([query])[0]

        # 2/3. Resolve k-NN and zero-shot with the two-threshold rule (see module docstring).
        knn = self._best(query_vec, self._example_vecs)  # nearest labelled example
        zs = self._best(query_vec, self._label_name_vecs)  # nearest label NAME (zero-shot)
        knn_sim = knn[1] if knn is not None else -1.0
        zs_sim = zs[1] if zs is not None else -1.0

        # (a) a confident example match wins outright.
        if knn is not None and knn_sim >= self._knn_trust:
            return self._knn_suggestion(knn)
        # (b) else a curated label name that clears the gate (robust at cold start).
        if zs is not None and zs_sim >= self._gate:
            idx, sim = zs
            name = self._label_names[idx]
            return CategorySuggestion(
                label=name, confidence=sim, provenance="zeroshot", evidence=name
            )
        # (c) else a weak example match is the last resort before abstaining.
        if knn is not None and knn_sim >= self._gate:
            return self._knn_suggestion(knn)

        # 4. Nothing cleared the gate — leave it for human review.
        return CategorySuggestion(label=None, confidence=0.0, provenance="none")

    def _knn_suggestion(self, knn: tuple[int, float]) -> CategorySuggestion:
        idx, sim = knn
        return CategorySuggestion(
            label=self._example_labels[idx],
            confidence=sim,
            provenance="knn",
            evidence=self._example_texts[idx],  # the matched example, for auditability
        )

    @staticmethod
    def _best(
        query_vec: NDArray[np.float32], ref_vecs: NDArray[np.float32]
    ) -> tuple[int, float] | None:
        """Return (index, cosine_sim) of the single nearest reference vector, or None if empty."""
        if ref_vecs.shape[0] == 0:
            return None
        sims = ref_vecs @ query_vec  # cosine: every vector is L2-normalised
        idx = int(np.argmax(sims))
        return idx, float(sims[idx])
