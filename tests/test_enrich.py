"""Unit tests for the Phase 2 enricher cascade, using the deterministic FakeEmbedder (no network).

The fake collapses strings to their lowercased token sets, so token overlap drives cosine
similarity — enough to exercise every branch of MCC -> k-NN -> zero-shot -> none without a model.
"""

from __future__ import annotations

from datetime import UTC, datetime

from firefly_bot.banking.mcc import category_for_mcc
from firefly_bot.enrich import Categoriser, FakeEmbedder
from firefly_bot.models import LabelRecord


def _example(counterparty: str, description: str, label: str) -> LabelRecord:
    return LabelRecord(
        ts=datetime(2026, 6, 10, tzinfo=UTC),
        kind="category",
        features={"counterparty_name": counterparty, "description": description},
        predicted=label,
        score=1.0,
        source="auto",
    )


def _categoriser(*, gate: float = 0.83) -> Categoriser:
    examples = [
        _example("Albert Heijn", "Boodschappen pinbetaling", "Boodschappen"),
        _example("Shell Station A2", "Tankbeurt", "Brandstof"),
        _example("Cafe Belgie", "Borrel", "Horeca"),
    ]
    # Energie / Telecom / Overheid have NAMES but no examples -> only reachable via zero-shot.
    inventory = ["Boodschappen", "Brandstof", "Horeca", "Energie", "Telecom", "Overheid"]
    return Categoriser(examples, inventory, FakeEmbedder(), gate=gate)


def test_mcc_path_wins_first() -> None:
    cat = _categoriser()
    # MCC 5411 maps to Boodschappen and must short-circuit before any embedding lookup.
    out = cat.suggest("Some Unknown Shop", "random description", mcc="5411")
    assert out.provenance == "mcc"
    assert out.label == "Boodschappen"
    assert out.confidence == 1.0
    assert out.evidence == "5411"


def test_knn_inherits_label_for_token_sharing_string() -> None:
    cat = _categoriser()
    # A different Albert Heijn presentation, no MCC: same meaningful tokens as the seeded AH
    # example (4 of 4 shared after dropping the store number) -> k-NN inherits its label.
    out = cat.suggest("Albert Heijn", "pinbetaling Boodschappen", mcc=None)
    assert out.provenance == "knn"
    assert out.label == "Boodschappen"
    assert out.confidence >= 0.83
    assert out.evidence == "Albert Heijn Boodschappen pinbetaling"


def test_zeroshot_assigns_label_with_name_but_no_examples() -> None:
    cat = _categoriser()
    # "Energie" has a name in the inventory but ZERO examples: only zero-shot can reach it.
    # Token set equals the label name exactly, so it clears the gate against the name vector.
    out = cat.suggest("Energie", "energie", mcc=None)
    assert out.provenance == "zeroshot"
    assert out.label == "Energie"
    assert out.evidence == "Energie"
    assert out.confidence >= 0.83


def test_dissimilar_string_returns_none() -> None:
    cat = _categoriser()
    out = cat.suggest("Xyzzy Quux Frobnicate", "wibble wobble", mcc=None)
    assert out.provenance == "none"
    assert out.label is None
    assert out.confidence == 0.0


def test_gate_boundary_respected() -> None:
    # A string sharing exactly one of two tokens with an example sits below a high gate.
    cat_strict = _categoriser(gate=0.99)
    out_strict = cat_strict.suggest("Albert Onbekend", "iets anders", mcc=None)
    assert out_strict.provenance == "none"

    # The very same input clears a permissive gate via k-NN (token overlap on "albert").
    cat_loose = _categoriser(gate=0.1)
    out_loose = cat_loose.suggest("Albert Onbekend", "iets anders", mcc=None)
    assert out_loose.provenance == "knn"
    assert out_loose.label == "Boodschappen"


def test_exact_token_match_scores_one() -> None:
    cat = _categoriser()
    # Identical token set to a seeded example -> cosine 1.0 (collision in the fake embedder).
    out = cat.suggest("Shell Station A2", "Tankbeurt", mcc=None)
    assert out.provenance == "knn"
    assert out.label == "Brandstof"
    assert out.confidence == 1.0


def test_category_for_mcc_integration() -> None:
    # The deterministic map the cascade's step 1 relies on.
    assert category_for_mcc("5411") == "Boodschappen"
    assert category_for_mcc("5541") == "Brandstof"
    assert category_for_mcc(None) is None
    assert category_for_mcc("0000") is None
