# Enrichment & ML roadmap — functional design

Status: living document. Phase 0/1 are specified to implementation level; Phase 2 is designed;
Phase 3/4 list known gaps to close before they can start. Each phase is refined as the prior
one lands.

## Principle

The industry pattern (Plaid Enrich, Yodlee, Ntropy, Meniga) is a **layered cascade**, not one
model:

```
cleanse → merchant identity (entity resolution) → categorize → recurring/frequency → confidence gate
```

For firefly-bot the cascade must be **local-first and private** (personal Dutch finances rule out
SaaS enrichers) and must fit the existing architecture: every I/O boundary is an injected
`Protocol` with a real default and a fake for tests, and `--dry-run` falls out of a write-suppressing
decorator. ML layers are added the same way — as Protocols — so they are testable and reversible.

Cheap-deterministic first (MCC map, Firefly rules, regex) → **local ML for the ambiguous middle**
(embeddings) → **LLM only for the residual** (local/Ollama, offline). We never pay LLM latency/cost
on the 90% that is easy.

### The feedback loop we already have

The audit report + `needs-review` tag + `DryRunLedger` are already a human-in-the-loop labelling
surface. Every auto-tag we apply and every correction the user makes in Firefly is a labelled
example. Phase 1 captures that signal; Phases 2–3 consume it. No separate labelling effort exists.

## New seams (Protocols, mirroring `TextRecogniser` / `Ledger` / `ReportWriter`)

- `LabelStore` (Phase 1): append-only record of every decision + later correction → `labels.jsonl`.
- `Enricher` (Phase 2): `categorise(txn) -> CategorySuggestion`; `resolve_merchant(name, iban) -> MerchantKey`.
- `MatchScorer` (Phase 3): `score(invoice, txn) -> float` — a learned replacement for the additive
  heuristic in `matching/matcher.py`, selected behind the same call site so the heuristic stays the
  fallback.
- `Recogniser` already exists; Phase 4 adds an optional `OllamaRecogniser` / vision fallback.

## Phases

| Phase | Deliverable | Depends on | Risk |
|---|---|---|---|
| 0 | Wrap `pycamt` behind `parse_camt053`; CLBD balance reconciliation | — | low |
| 1 | `LabelStore` → `labels.jsonl` capture in pipeline + importer | — | low |
| 2 | Local embedding `Enricher`: categoriser + merchant entity resolution | 1 (data), embeddings spike | med |
| 3 | Learned `MatchScorer` for invoice↔transaction | 1 (labels incl. negatives), 2 (embedding feature) | med |
| 4 | Recurring detection + local LLM/vision fallback | 2 (canonical merchant), Firefly recurring API, Ollama | high |

## Phase 0 — DONE (outcome differs from original plan)

**pycamt was evaluated and rejected.** Empirically pycamt 1.0.1 **cannot parse the Dutch
`camt.053.001.02` dialect** (it reads `ValDt//Dt` unconditionally; our entries book with `BookgDt`
only → `AttributeError`), and even where it parses it drops entry amount, counterparty IBAN, bank
reference, and typed/signed balances — every field we depend on. It was strictly worse than the
hand-rolled `xml.etree` parser. Backed out: no `pycamt` dependency, etree parser retained.

**Kept:** `BankStatement.closing_balance` (CLBD), `reconciles(statement)` (opening + Σ signed
entries == closing; None when a balance is absent), a warn-don't-raise on mismatch in
`parse_camt053`, and `ImportSummary.reconciled: bool | None`. 45 tests pass, mypy/ruff clean.

**Stability goal carried forward** (the real reason we looked at pycamt): pursue via
**XSD validation + golden-file tests per bank**, not a library. Open follow-up:
- Decide on an `lxml` dependency for true XSD validation (stdlib ElementTree can't validate).
- Collect one anonymised real statement per bank (ABN AMRO / RegioBank / ASN) as golden fixtures.
  *Needs sample statements from the operator.*

## Phase 1 — label capture

**1a (emission) — DONE.** `LabelRecord` model, `LabelStore` Protocol + `JsonlLabelStore` (real,
`./data/labels.jsonl`) + `NullLabelStore` (dry-run/tests). `matching/matcher.py` gained a pure
`score_candidates()` returning every positive candidate + its feature dict; `match_invoice` is
rewired through it with byte-identical behaviour. Pipeline emits one `match` record per candidate
(winner + losers, `chosen` flag, `candidate_id`); importer emits `category` + `merchant` records per
transaction. `data_dir` setting added; `data/` gitignored. `corrected` always None (that's 1b).
50 tests pass, mypy/ruff clean. (Review fix: a non-hermetic test was writing to the repo's real
`./data`; `_settings()` test helper now points `data_dir` at a temp dir.)

**1b (correction capture) — NEXT.** Fill `corrected` from ground truth:
- On each run, before processing, load prior `match`/`category`/`merchant` records whose `corrected`
  is still None, read the *current* Firefly state for those transactions (tags/category), and append
  a follow-up record (or update) with `corrected` set + `source="user"` where the human changed what
  we predicted. Needs read access via the existing `Ledger`/client — testable with the existing fakes.
- Open design Qs to resolve against the 1a output: append-only correction records vs. in-place update
  of the JSONL; how to key a prior record back to a Firefly transaction (we store `candidate_id` /
  account id, so keyable); debounce so a transaction isn't re-evaluated forever.

### 1a record schema (as implemented)

`LabelRecord` (new pydantic model, append-only JSONL at `./data/labels.jsonl`):

```
schema_version: int
ts: datetime
kind: "match" | "category" | "merchant"
# inputs (raw, so a future model can re-featurise)
features: dict[str, str | float | bool | None]
# what we decided automatically, and the confidence
predicted: str | None
score: float
# the ground truth, filled in later from Firefly state diff or xlsx round-trip (None until known)
corrected: str | None
source: "auto" | "user"
```

- `match`: one record per candidate considered (not just the winner) so Phase 3 gets **negatives**.
  features = amount delta, signed date delta, IBAN match, invoice-number-in-description, etc.
- `category` / `merchant`: emitted by the importer per transaction.
- Correction capture: on each run, diff the prior decisions against current Firefly state (tag/category
  changes) and write `corrected`. No model yet — this phase only accumulates data.

## Phase 2 — local embedding enricher (design, refined by the spike)

**Status: 2.1 (categoriser cascade) and 2.2 (new-label discovery + active-learning order) DONE,**
verified with the real e5 model (see `experiments/enrich_demo.py`, `experiments/discovery_demo.py`).
Remaining: bootstrap importer, merchant entity resolution (2.3), wiring into the write path (2.4).
Calibration learned in build: classify uses a two-threshold rule (confident k-NN ≥0.90 wins, else
gated zero-shot ≥0.83, else weak k-NN is a review-only suggestion); discovery clusters at ≥0.90
(e5's high floor chains below that). All knobs live in `EnrichSettings`.

**Spike result (`experiments/EMBEDDINGS_SPIKE.md`):** use **`fastembed` + `intfloat/multilingual-e5-small`**
(384-dim, **MIT**). Measured ~**4.1 ms/embed** single (~1.4 ms batched) on CPU/Windows; **6/6** on
held-out Dutch merchants. fastembed reuses our existing `onnxruntime`/`numpy` pins and adds **no
torch** (~24 MB net vs ~+400–500 MB for sentence-transformers). e5 needs `query:`/`passage:`
prefixes and MEAN pooling + L2 norm via `add_custom_model` (e5-small isn't in fastembed's catalogue
but its HF repo ships `onnx/model.onnx`).

- Categoriser: embed `passage: {counterparty_name} {description}`; **k-NN over `labels.jsonl`**
  vectors (cosine over a few-thousand-row numpy matrix — instant, no vector DB).
- **Confidence gate: threshold on absolute `top_sim` (~> 0.83), NOT the margin** — the spike found e5
  gives high baseline similarities with small inter-category margins (0.01–0.07). Below threshold →
  existing `needs-review`.
- Merchant entity resolution: replace `AccountResolver`'s `rapidfuzz` step with embedding-nearest to
  existing-account centroids (collapses "Albert Heijn 2264/2277", "AH to go"). Keep IBAN-exact as the
  first, cheapest check; keep `rapidfuzz` as a fallback for the cold-start before labels exist.

**Scope expanded — label *discovery*, not just classification (see `docs/COLD_START.md`).** Phase 2
must work on a fresh dataset (a couple of months, imported then run immediately) and must propose
labels that don't exist yet. Adds to the above:
- **MCC seeding + zero-shot:** deterministic MCC→category labels (day-one, no data) become examples;
  classification also runs zero-shot against label *names*, so a newly-named label is usable before it
  has any examples.
- **Label inventory:** an evolving set of category names (seeded from MCC map / Firefly history / user),
  not a fixed taxonomy.
- **New-label discovery:** cluster below-the-gate "orphan" embeddings; a recurring cluster becomes a
  candidate new category, named heuristically (or by the Phase 4 LLM) and surfaced for one-click
  accept/rename; on accept its members anchor future runs.
- **Bootstrap importer (cold-start booster):** a one-off, read-only export of existing categorised
  Firefly transactions into `labels.jsonl` to warm-start. Highest-leverage user-supplied data.
- **Active-learning review order:** rank `needs-review` by impact × uncertainty so few corrections go far.
- Open follow-up: out-of-band int8 quantisation of the e5 ONNX (fp32 graph is 448 MB on disk; fine
  at runtime but heavy to ship) — fastembed's loader didn't fetch the repo's pre-quantised variant.

## Phase 3 — learned match scorer — KNOWN GAPS (must close before starting)

1. **Negative examples.** The pipeline records only the chosen match. Phase 1 `match` records must
   log the *whole candidate set* with the chosen flag, or there is nothing to train a discriminator on.
2. **Cold start.** With no history the scorer has no training data. Need: keep the additive heuristic
   as fallback until N≥(threshold) confirmed labels exist; consider bootstrapping from the heuristic's
   own high-confidence outputs as weak labels.
3. **Shared, versioned feature extractor.** One typed function producing the feature vector, used by
   both training and inference, with a `schema_version` so old labels stay usable. Today the features
   live implicitly inside `_score`.
4. **Embedding feature dependency.** The strongest new feature (merchant-name embedding similarity)
   needs Phase 2 shipped → Phase 3 depends on Phase 2.
5. **Model persistence + fallback.** Where the scikit-learn model lives, load path, retrain trigger,
   and graceful fallback to the heuristic when the model is missing/stale.
6. **Threshold calibration + eval harness.** Calibrated probability → the auto-attach vs needs-review
   cut needs a held-out validation set; need precision/recall reporting to *prove* it beats the
   heuristic before switching the default.
7. **Label quality / leakage.** Corrections must be attributable to the right candidate; guard against
   training on auto-decisions the user never actually confirmed.

## Phase 4 — recurring detection + LLM fallback — KNOWN GAPS

1. **Cross-run history store.** Recurring detection needs transaction history grouped by *canonical
   merchant* (Phase 2). No persistent history exists today beyond Firefly itself.
2. **Periodicity algorithm + minimum occurrences.** Interval/gap clustering with a min-occurrence
   gate; cold-start again (can't call something monthly from one instance).
3. **Firefly recurring API.** `FireflyClient` has no recurring-transaction endpoints; need to add
   them and model Firefly's recurrence (type, repetitions, repeat_until) — plus idempotency so a
   re-run doesn't create duplicate recurrence definitions.
4. **Local LLM runtime (Ollama).** Not present. Need an adapter behind the recogniser/enricher seam,
   a quantized **Dutch-capable** model choice, a prompt + **pydantic-validated structured output**
   contract, and routing that only sends the residual `needs-review` tail to it.
5. **Vision-LLM extraction.** Reuse the pypdfium2 rasterisation path; add a vision model + a
   verification/confidence step before trusting its fields.
6. **Privacy + offline guarantee.** A test/guard that the LLM path makes no external network calls.
7. **Hardware gate.** PLAN.md says don't buy hardware until the pipeline is proven; a local vision
   model may need a GPU — explicit decision point, not an assumption.
8. **Cost/latency budget.** The confidence gates from Phases 2–3 are the routing mechanism that keeps
   LLM usage to the small residual.
