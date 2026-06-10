"""Unit tests for Phase 2.2 new-label discovery + active-learning review order.

Uses the deterministic FakeEmbedder (no network): it collapses each string to its lowercased
token set and hashes tokens into a vector, so strings that share tokens land close in cosine
space and strings that share *all* tokens collide exactly. That is enough to exercise clustering,
cohesion, naming, and the review-order priority without a model.
"""

from __future__ import annotations

from firefly_bot.enrich import FakeEmbedder, discover_labels, review_order


def test_two_token_sharing_groups_form_two_candidates() -> None:
    # Two civic-fee orphans + three parking orphans, each group sharing a strong token, plus one
    # unrelated one-off. The two groups should crystallise into two candidate labels.
    texts = [
        "Gemeente Utrecht leges",
        "Gemeente Amsterdam paspoort",
        "Gemeente Rotterdam rijbewijs",
        "Parkeren centrum kort",
        "Parkeren straat lang",
        "Parkeren station avond",
        "Spotify maandabonnement music",  # one-off -> must NOT form a candidate
    ]
    candidates = discover_labels(texts, FakeEmbedder(), min_size=3, threshold=0.25)

    assert len(candidates) == 2
    names = {c.suggested_name for c in candidates}
    # Names derive from the dominant shared, normalised-merchant token in each cluster.
    assert "Gemeente" in names
    assert "Parkeren" in names
    # The lonely Spotify orphan never reached min_size, so it is absent.
    assert all("spotify" not in t.lower() for c in candidates for t in c.member_texts)


def test_sorted_by_size_descending() -> None:
    texts = [
        # A 4-member group...
        "Gemeente Utrecht leges",
        "Gemeente Amsterdam leges",
        "Gemeente Rotterdam leges",
        "Gemeente Den Haag leges",
        # ...and a 3-member group.
        "Parkeren centrum een",
        "Parkeren centrum twee",
        "Parkeren centrum drie",
    ]
    candidates = discover_labels(texts, FakeEmbedder(), min_size=3, threshold=0.3)
    assert [c.size for c in candidates] == [4, 3]
    assert candidates[0].suggested_name == "Gemeente"


def test_singleton_does_not_form_candidate() -> None:
    # A single distinctive orphan is noise, not evidence — min_size drops it.
    texts = ["Eenmalige Zeldzame Transactie xyz"]
    assert discover_labels(texts, FakeEmbedder(), min_size=3) == []

    # Even two near-identical orphans stay below the default min_size of 3.
    pair = ["Gemeente leges aanvraag", "Gemeente leges aanvraag"]
    assert discover_labels(pair, FakeEmbedder(), min_size=3) == []


def test_cohesion_in_unit_interval_and_higher_for_tighter_cluster() -> None:
    # Tight cluster: identical token sets -> the fake collides them exactly -> cohesion == 1.0.
    tight = ["Parkeren Q-Park", "Parkeren Q-Park", "Parkeren Q-Park"]
    tight_candidates = discover_labels(tight, FakeEmbedder(), min_size=3, threshold=0.3)
    assert len(tight_candidates) == 1
    tight_cohesion = tight_candidates[0].cohesion

    # Loose cluster: members share one anchor token but each adds distinct tokens -> lower cosine.
    loose = [
        "Gemeente alpha beta gamma",
        "Gemeente delta epsilon zeta",
        "Gemeente eta theta iota",
    ]
    loose_candidates = discover_labels(loose, FakeEmbedder(), min_size=3, threshold=0.1)
    assert len(loose_candidates) == 1
    loose_cohesion = loose_candidates[0].cohesion

    for cohesion in (tight_cohesion, loose_cohesion):
        assert 0.0 <= cohesion <= 1.0
    assert tight_cohesion == 1.0
    assert tight_cohesion > loose_cohesion


def test_empty_input_returns_no_candidates() -> None:
    assert discover_labels([], FakeEmbedder()) == []


def test_review_order_prioritises_frequent_low_confidence_item() -> None:
    # A frequently-recurring low-confidence merchant (3x) vs a one-off higher-confidence orphan.
    # Settling the recurring one labels three transactions at once -> it must come first.
    items = [
        ("Eenmalige rare betaling", 0.70),  # one-off, higher confidence -> lower priority
        ("Gemeente leges aanvraag", 0.40),
        ("Gemeente leges aanvraag", 0.40),
        ("Gemeente leges aanvraag", 0.40),
    ]
    order = review_order(items)
    # All three recurring Gemeente items (indices 1,2,3) rank ahead of the one-off (index 0).
    assert order[-1] == 0
    assert set(order[:3]) == {1, 2, 3}


def test_review_order_uncertainty_breaks_ties_within_same_frequency() -> None:
    # Two distinct one-offs (frequency 1 each): the more uncertain (lower confidence) goes first.
    items = [
        ("Alpha unieke betaling", 0.80),  # less uncertain
        ("Beta andere betaling", 0.20),  # more uncertain -> first
    ]
    assert review_order(items) == [1, 0]


def test_review_order_is_deterministic_and_stable_on_equal_priority() -> None:
    # Equal priority (same confidence, same frequency) -> preserve original input order.
    items = [
        ("Alpha betaling", 0.50),
        ("Beta betaling", 0.50),
    ]
    assert review_order(items) == [0, 1]


def test_review_order_empty() -> None:
    assert review_order([]) == []
