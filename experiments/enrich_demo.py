"""Live demo of the Phase 2 enricher with the REAL e5 embedder (no fakes, no network at run time).

Seeds ~3 labelled examples + a 6-category Dutch inventory, then classifies ~12 realistic Dutch
transactions that exercise ALL FOUR provenance paths (mcc / knn / zeroshot / none) and prints a
table of: input | label | confidence | provenance | action (auto vs needs-review).

Then demonstrates the density loop: add ONE confirmed example and re-run to show a cold-start
"review" become a confident auto match — and that same correction lifting a never-seen merchant.

Run (from repo root, project venv):
    .\\.venv\\Scripts\\python.exe experiments\\enrich_demo.py

Typed; passes `mypy --strict` and `ruff check`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from firefly_bot.config import EnrichSettings
from firefly_bot.enrich import Categoriser, E5Embedder
from firefly_bot.models import CategorySuggestion, LabelRecord


def example(counterparty: str, description: str, label: str) -> LabelRecord:
    return LabelRecord(
        ts=datetime(2026, 6, 10, tzinfo=UTC),
        kind="category",
        features={"counterparty_name": counterparty, "description": description},
        predicted=label,
        score=1.0,
        source="auto",
    )


# ~3 labelled examples (the k-NN reference set). Deliberately a handful — cold start.
EXAMPLES: list[LabelRecord] = [
    example("Albert Heijn 1102", "Boodschappen pinbetaling", "Boodschappen"),
    example("Shell Station A2", "Tankbeurt brandstof", "Brandstof"),
    example("Cafe Belgie Utrecht", "Borrel en eten", "Horeca"),
]

# Six Dutch category names. Energie / Telecom / Overheid have NAMES but NO examples yet,
# so they are only reachable via zero-shot against the name.
INVENTORY: list[str] = [
    "Boodschappen",
    "Brandstof",
    "Energie",
    "Telecom",
    "Horeca",
    "Overheid",
]


# (counterparty, description, mcc) — a mix that hits every provenance path.
TRANSACTIONS: list[tuple[str, str, str | None]] = [
    # MCC path: a grocery card payment tagged 5411.
    ("Albert Heijn 2264", "Pasbetaling", "5411"),
    ("Esso Tankstation", "Brandstof", "5541"),  # MCC fuel
    # k-NN path: another Albert Heijn store, no MCC -> inherits via the seeded AH example.
    ("Albert Heijn 1532", "Boodschappen pinbetaling", None),
    ("Jumbo Supermarkten", "Boodschappen", None),  # k-NN to AH (or zero-shot to Boodschappen)
    ("Shell Express", "Tankbeurt brandstof", None),  # k-NN to Shell example
    ("Grand Cafe Central", "Borrel en eten", None),  # k-NN to the Horeca example
    # zero-shot path: named-but-exampleless categories matched by their name.
    ("Eneco", "Energie levering maandtermijn", None),  # -> Energie (no example)
    ("Vattenfall", "Stroom en gas energie", None),  # -> Energie (no example)
    ("KPN", "Telecom abonnement mobiel", None),  # -> Telecom (no example)
    ("Gemeente Utrecht", "Overheid leges aanvraag", None),  # -> Overheid (no example)
    ("Vodafone", "Telefonie abonnement data", None),  # -> Telecom (no example), zero-shot
    # orphan: a generic bank-mechanics string that resembles no category name or example ->
    # top similarity stays below the 0.83 gate -> none / needs-review (the bot never guesses).
    ("Incasso machtiging 7781", "afschrijving", None),
]


def action(suggestion: CategorySuggestion, *, gate: float, knn_trust: float) -> str:
    """The write-path rule: auto-apply only confident suggestions, else send to review.

    Deterministic MCC, a *confident* k-NN (>= knn_trust), or a gated zero-shot label name are
    trusted enough to write automatically; a weak k-NN (a near-floor example match) is only a
    suggestion and is routed to needs-review — the bot never auto-writes a guess it isn't sure of.
    """
    if suggestion.provenance == "mcc":
        return "auto"
    if suggestion.provenance == "knn" and suggestion.confidence >= knn_trust:
        return "auto"
    if suggestion.provenance == "zeroshot" and suggestion.confidence >= gate:
        return "auto"
    return "review"


def _print_row(
    label_in: str, s: CategorySuggestion, *, gate: float, knn_trust: float
) -> None:
    shown = label_in if len(label_in) <= 43 else label_in[:42] + "."
    label = s.label if s.label is not None else "(needs-review)"
    act = action(s, gate=gate, knn_trust=knn_trust)
    print(f"{shown:<44} {label:<16} {s.confidence:>6.3f}  {s.provenance:<9} {act}")


def main() -> None:
    settings = EnrichSettings()
    gate, knn_trust = settings.gate, settings.knn_trust
    embedder = E5Embedder()
    categoriser = Categoriser(EXAMPLES, INVENTORY, embedder, gate=gate, knn_trust=knn_trust)

    header = f"{'input':<44} {'label':<16} {'conf':>6}  {'prov':<9} action"
    print(f"COLD START - {len(EXAMPLES)} examples, gate={gate}, knn_trust={knn_trust}")
    print(header)
    print("-" * len(header))
    for counterparty, description, mcc in TRANSACTIONS:
        s = categoriser.suggest(counterparty, description, mcc)
        _print_row(f"{counterparty} | {description}", s, gate=gate, knn_trust=knn_trust)

    # --- density loop: ONE correction teaches the bot, and generalises to a NEW merchant -------
    # Eneco was a weak k-NN -> review at cold start (no Energie example, name just under the gate).
    # Add a single confirmed "Eneco -> Energie" example and re-run: Eneco becomes a confident k-NN
    # (auto), and a *previously unseen* energy supplier (Essent) is lifted by that same example.
    warmed = Categoriser(
        [*EXAMPLES, example("Eneco", "Energie levering maandtermijn", "Energie")],
        INVENTORY,
        embedder,
        gate=gate,
        knn_trust=knn_trust,
    )
    print(f"\nAFTER 1 CORRECTION - {len(EXAMPLES) + 1} examples (added Eneco -> Energie)")
    print(header)
    print("-" * len(header))
    for counterparty, description, mcc in [
        ("Eneco", "Energie maandtermijn", None),  # was review -> now confident k-NN
        ("Essent", "Energie en gas levering", None),  # NEVER seen -> lifted by the Eneco example
    ]:
        s = warmed.suggest(counterparty, description, mcc)
        _print_row(f"{counterparty} | {description}", s, gate=gate, knn_trust=knn_trust)


if __name__ == "__main__":
    main()
