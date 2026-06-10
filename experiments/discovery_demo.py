"""Live demo of Phase 2.2 new-label discovery with the REAL e5 embedder (no fakes, no fake network).

Feeds ~10 orphan strings — a few civic-fee variants, a few parking variants, and a couple of true
one-offs — to ``discover_labels`` and prints the candidate new categories it crystallises (name,
size, cohesion). Then runs ``review_order`` over a needs-review list to show the active-learning
order: a frequently-recurring low-confidence item is surfaced ahead of a confident one-off.

Run (from repo root, project venv):
    .\\.venv\\Scripts\\python.exe experiments\\discovery_demo.py

Typed; passes `mypy --strict` and `ruff check`.
"""

from __future__ import annotations

from firefly_bot.enrich import E5Embedder, discover_labels, review_order

# ~10 orphan texts ("{counterparty} {description}") the categoriser could not place. Two recurring
# themes (civic fees, parking) plus two genuine one-offs that should NOT crystallise.
ORPHANS: list[str] = [
    "Gemeente Utrecht leges paspoort",
    "Gemeente Amsterdam leges rijbewijs",
    "Gemeente Rotterdam leges uittreksel",
    "Gemeente Eindhoven leges aanvraag",
    "Q-Park parkeergarage Centraal",
    "Q-Park parkeergarage Noord",
    "Q-Park parkeergarage West",
    "Tikkie terugbetaling etentje",  # one-off
    "Bol.com bestelling boek",  # one-off
    "Netflix maandelijks abonnement",  # one-off
]

# needs-review (text, confidence): a recurring low-confidence parking orphan (same normalised
# merchant, appears 3x) vs one-offs with higher confidence. Active learning weights uncertainty by
# frequency, so the recurring uncertain item is surfaced first — settling three rows at once.
NEEDS_REVIEW: list[tuple[str, float]] = [
    ("Bol.com bestelling boek", 0.72),
    ("Q-Park parkeergarage 0231", 0.45),
    ("Q-Park parkeergarage 0498", 0.45),
    ("Q-Park parkeergarage 7712", 0.45),
    ("Tikkie terugbetaling etentje", 0.66),
]


def main() -> None:
    embedder = E5Embedder()

    # threshold=0.90 (above the 0.86 API default): real e5 has a high similarity floor with small
    # inter-class margins, so single-link clustering needs a higher bar to keep distinct themes
    # apart (at 0.86 every short Dutch string chains into one blob). See the cosine matrix in the
    # spike notes; 0.90 cleanly separates civic-fees from parking and drops the one-offs.
    print(f"DISCOVER LABELS - {len(ORPHANS)} orphans, min_size=3, threshold=0.90")
    header = f"{'suggested_name':<16} {'size':>4}  {'cohesion':>8}  members"
    print(header)
    print("-" * len(header))
    for candidate in discover_labels(ORPHANS, embedder, min_size=3, threshold=0.90):
        members = " | ".join(candidate.member_texts)
        print(
            f"{candidate.suggested_name:<16} {candidate.size:>4}  "
            f"{candidate.cohesion:>8.3f}  {members}"
        )

    print(f"\nREVIEW ORDER - {len(NEEDS_REVIEW)} needs-review items (impact x uncertainty)")
    rank_header = f"{'rank':>4}  {'conf':>5}  text"
    print(rank_header)
    print("-" * len(rank_header))
    for rank, index in enumerate(review_order(NEEDS_REVIEW), start=1):
        text, confidence = NEEDS_REVIEW[index]
        print(f"{rank:>4}  {confidence:>5.2f}  {text}")


if __name__ == "__main__":
    main()
