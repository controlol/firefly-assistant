"""Throwaway probe: can e5 separate merchant *variants* from *distinct* merchants at the gate?

Phase 2.3 (docs/ENRICHMENT.md) gates an OPT-IN embedding step in ``AccountResolver`` at
``EnrichSettings.merchant_gate`` (default 0.93). A false MERGE of two distinct merchants corrupts
the ledger, so the gate is only safe if real merchant *variants* (e.g. "Albert Heijn" / "AH to go"
/ "Appie") score ABOVE it against their canonical name while *distinct* merchants ("Jumbo", "Shell",
"KPN") score clearly BELOW it. This script measures both with the REAL e5 embedder and prints the
cosines so the gate can be judged empirically.

Run (from repo root, project venv):
    .\\.venv\\Scripts\\python.exe experiments\\merchant_resolution_probe.py

Typed; passes `mypy --strict` and `ruff check`.
"""

from __future__ import annotations

from firefly_bot.banking.accounts import normalise_merchant
from firefly_bot.config import EnrichSettings
from firefly_bot.enrich import E5Embedder

# Canonical accounts already known to the ledger (one per real merchant).
CANONICALS: list[str] = [
    "Albert Heijn",
    "Jumbo",
    "Shell",
    "KPN",
    "Eneco",
    "Coolblue",
]

# Spelling/brand VARIANTS of Albert Heijn that SHOULD merge onto its canonical account.
VARIANTS: list[tuple[str, str]] = [
    ("AH to go", "Albert Heijn"),
    ("Appie", "Albert Heijn"),
    ("Albert Heijn to go 2264", "Albert Heijn"),
    ("AH XL Utrecht", "Albert Heijn"),
    ("Albert Heijn Online", "Albert Heijn"),
    ("AH Bezorgservice", "Albert Heijn"),
]

# DISTINCT merchants that MUST NOT merge onto Albert Heijn (the dangerous false-merge case).
DISTINCT: list[tuple[str, str]] = [
    ("Jumbo Supermarkten", "Albert Heijn"),
    ("Shell Tankstation", "Albert Heijn"),
    ("KPN Mobiel", "Albert Heijn"),
    ("Eneco Energie", "Albert Heijn"),
    ("Coolblue", "Albert Heijn"),
    ("Lidl", "Albert Heijn"),
]


def main() -> None:
    gate = EnrichSettings().merchant_gate
    embedder = E5Embedder()

    # Embed each canonical name once (passage side), as AccountResolver caches the known names.
    canon_norms = [normalise_merchant(c) for c in CANONICALS]
    canon_vecs = embedder.embed_passages(canon_norms)
    canon_index = {c: i for i, c in enumerate(CANONICALS)}

    def cosine_to(name: str, canonical: str) -> float:
        q = embedder.embed_queries([normalise_merchant(name)])[0]
        return float(canon_vecs[canon_index[canonical]] @ q)

    print(f"merchant_gate = {gate}\n")

    print(f"{'VARIANT (should be >= gate)':<34} {'canonical':<14} cosine  verdict")
    print("-" * 70)
    variant_hits = 0
    for name, canonical in VARIANTS:
        sim = cosine_to(name, canonical)
        ok = sim >= gate
        variant_hits += int(ok)
        print(f"{name:<34} {canonical:<14} {sim:>6.3f}  {'MERGE' if ok else 'miss (dup)'}")

    print(f"\n{'DISTINCT (must be < gate)':<34} {'canonical':<14} cosine  verdict")
    print("-" * 70)
    false_merges = 0
    for name, canonical in DISTINCT:
        sim = cosine_to(name, canonical)
        bad = sim >= gate
        false_merges += int(bad)
        print(f"{name:<34} {canonical:<14} {sim:>6.3f}  {'FALSE MERGE!' if bad else 'kept apart'}")

    print(
        f"\nSummary: {variant_hits}/{len(VARIANTS)} variants merge at gate, "
        f"{false_merges}/{len(DISTINCT)} distinct merchants FALSELY merge."
    )
    if false_merges:
        print("=> Gate is NOT safe: distinct merchants clear it. Keep merchant_resolution OFF.")


if __name__ == "__main__":
    main()
