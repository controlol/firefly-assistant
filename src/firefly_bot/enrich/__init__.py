"""Local embedding enricher (Phase 2): categorise transactions privately, no torch, no cloud.

See docs/ENRICHMENT.md (Phase 2) and docs/COLD_START.md for the design, and
experiments/EMBEDDINGS_SPIKE.md for the model/runtime choice.
"""

from __future__ import annotations

from firefly_bot.enrich.bootstrap import BootstrapSummary, bootstrap_labels
from firefly_bot.enrich.categoriser import Categoriser
from firefly_bot.enrich.corrections import CorrectionSummary, capture_corrections
from firefly_bot.enrich.discovery import discover_labels, review_order
from firefly_bot.enrich.embedder import E5Embedder, Embedder, FakeEmbedder

__all__ = [
    "BootstrapSummary",
    "Categoriser",
    "CorrectionSummary",
    "E5Embedder",
    "Embedder",
    "FakeEmbedder",
    "bootstrap_labels",
    "capture_corrections",
    "discover_labels",
    "review_order",
]
