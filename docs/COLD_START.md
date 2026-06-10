# Auto-labelling & cold start — how the bot learns from little data

This document explains how firefly-bot assigns categories/labels to transactions **even when those
labels don't exist yet**, why it works after only a couple of months of imported data, the exact
steps it takes on each run, how it sharpens over time, and what *you* can do to give a fresh dataset
a strong head start.

It is a design document for the enrichment layer (Phase 2 of `docs/ENRICHMENT.md`), refined to cover
**label discovery**, not just classification into a fixed taxonomy.

## The problem in one paragraph

A brand-new user imports, say, three months of bank transactions and runs the bot immediately. There
is almost no labelled history to learn from, and the "right" set of categories isn't defined yet.
A naive classifier would either refuse to do anything useful, or force every transaction into a
generic, foreign taxonomy. We want it to (a) be useful on day one, (b) **propose new labels it has
never seen** when the data calls for them, and (c) get measurably better every time you correct it —
without ever training a large model or sending your data anywhere.

## Why a couple of months is enough

Personal finance is not a uniform distribution — it is dominated by a small number of repeating
things. That is what makes cold start tractable:

- **Card payments carry an MCC** (Merchant Category Code, e.g. `5411` = supermarkets). The bot
  already extracts these (`banking/camt.py` → `banking/mcc.py: category_for_mcc`). MCC gives a
  *correct category with zero training data* for a large share of everyday spend.
- **Spending is dominated by recurring merchants.** Albert Heijn, your energy supplier, your telecom,
  your landlord — a couple of months already contains most of the merchants you'll ever see. Once a
  merchant is labelled once, every future transaction from it inherits the label.
- **Labels have meaningful names.** "Boodschappen", "Brandstof", "Energie" are themselves
  descriptive. A multilingual embedding model can match a transaction to a *label name* it has never
  seen an example of (zero-shot) — so a label is usable the moment it's named, before any examples
  accumulate.

So the long tail that genuinely needs learning is small. The bot covers the bulk deterministically
(MCC + merchant consistency), uses zero-shot for newly-named labels, and only the residual ambiguous
minority needs your input — once.

## What "the data" is (recap)

The canonical store is one append-only file, `./data/labels.jsonl`, of `LabelRecord`s (see
`docs/ENRICHMENT.md`). Everything else — the embedding vectors, any small scorer — is a regenerable
derivative. On top of it the bot maintains a **label inventory**: the evolving set of category names
it knows about, each with the examples and (later) embeddings that anchor it. The inventory is seeded
and grown, never fixed.

## The steps the program takes on each run

```
                      ┌─────────────────────────────────────────────────────────┐
  new transactions ──►│ 1. DETERMINISTIC pre-label (no ML, no data needed)       │
                      │    MCC → category; existing Firefly rule/category        │
                      └───────────────┬─────────────────────────────────────────┘
                                      │ still unlabelled / low-trust
                      ┌───────────────▼─────────────────────────────────────────┐
                      │ 2. EMBED  passage: "{counterparty} {description}"  (e5)   │
                      └───────────────┬─────────────────────────────────────────┘
                                      │
                      ┌───────────────▼─────────────────────────────────────────┐
                      │ 3. CLASSIFY against what we already know                  │
                      │    (a) k-NN vs labelled examples      → label, top_sim   │
                      │    (b) zero-shot vs label *names*      → label, sim       │
                      │    take the stronger; if sim ≥ gate → assign (auto)      │
                      │                       else → needs-review (don't guess)  │
                      └───────────────┬─────────────────────────────────────────┘
                                      │ the "orphans" (below the gate)
                      ┌───────────────▼─────────────────────────────────────────┐
                      │ 4. DISCOVER NEW LABELS                                    │
                      │    cluster orphan embeddings; a recurring cluster of ≥N   │
                      │    becomes a *candidate new category*; propose a name;    │
                      │    surface for one-click accept/rename                    │
                      └───────────────┬─────────────────────────────────────────┘
                                      │
                      ┌───────────────▼─────────────────────────────────────────┐
                      │ 5. RECORD every decision + confidence + provenance to     │
                      │    labels.jsonl  (mcc | knn | zeroshot | cluster | user)  │
                      └─────────────────────────────────────────────────────────┘
```

**Step 1 — Deterministic pre-label.** If the transaction is a card payment with an MCC, map it to a
category. If a Firefly rule already set a category, respect it. These are high-confidence labels that
cost nothing and, crucially, **become labelled examples** that help everything below.

**Step 2 — Embed.** Compute one vector for `passage: {counterparty_name} {description}` using
`multilingual-e5-small` via fastembed (≈4 ms, local, no torch — see `experiments/EMBEDDINGS_SPIKE.md`).
All known labelled examples and all label *names* are embedded once and cached in memory.

**Step 3 — Classify against what we know.** Two lookups:
- **(a) k-NN over examples:** nearest labelled example → its label, with `top_sim`.
- **(b) zero-shot over label names:** nearest label *name* → that label, with similarity. This is what
  lets a label with **no examples yet** still be assigned the moment it's named.

Resolve the two with a **two-threshold rule** (a first cut that just "took the stronger cosine" was
wrong — implementation showed k-NN-vs-example and zero-shot-vs-name similarities aren't on the same
scale, so a spurious near-floor example, e.g. `Eneco → Brandstof` at ~0.84, outranks the correct label
name `Energie`). Instead:
- a **confident** k-NN (sim ≥ `knn_trust`, ≈0.90) wins outright — a strong example match carries
  merchant-specific knowledge a bare name can't;
- else a **gated** zero-shot label name (sim ≥ `gate`, ≈0.83) wins — curated names are robust even
  with zero examples and don't suffer the tiny-example noise;
- else a **weak** k-NN is only a *suggestion*.

Both thresholds are **absolute `top_sim`**, not margins (e5 has small inter-class margins). The
**write-path then gates on confidence**: MCC, confident k-NN, and gated zero-shot are auto-applied;
a weak k-NN or anything below the gate is left **`needs-review`** — the bot never auto-writes a guess
it isn't sure of, so it can't make things *worse* than today's deterministic system. As examples
accumulate (the density loop), confident k-NN fires more often and the rule relaxes itself.

**Step 4 — Discover new labels (the "labels that don't exist yet" part).** The transactions that fell
below the gate are *orphans*. We cluster their embeddings (cosine-threshold / agglomerative). When a
cluster of similar orphans is large/recurring enough, it's a **candidate new category** — the data is
telling us a category is missing. We propose a name:
- cheap heuristic now: the dominant normalised merchant or shared token in the cluster (e.g. five
  unlabelled "Shell"/"Tango"/"BP" orphans → propose "Brandstof");
- better later (Phase 4): a local LLM names the cluster from its members.

The candidate is surfaced in the audit report for one-click **accept / rename / reject**. On accept,
the new label joins the inventory and its members become labelled examples — so the next run classifies
the rest of that group confidently.

**Step 5 — Record.** Every decision is written to `labels.jsonl` with its confidence and **provenance**
(`mcc`, `knn`, `zeroshot`, `cluster`, or `user`), so we can always audit *why* a label was applied and
so a future scorer can weight sources differently.

### Worked example (first run, ~3 months imported)

- `Albert Heijn 2264 … MCC:5411` → **Step 1** labels it `Boodschappen` from MCC. Free, correct.
- `Albert Heijn 1102` (no MCC on this one) → **Step 3a** k-NN finds the just-labelled AH example,
  `top_sim` high → `Boodschappen`. Merchant consistency, no user input.
- `STADSARCHIEF LEGES` (a one-off you've never had) → below the gate → **needs-review**. It sits as an
  orphan. If three similar civic-fee orphans appear, **Step 4** proposes a new "Overheid/Leges" label
  for you to accept.
- Your energy supplier, telecom, rent → labelled once (by you or by name-based zero-shot), then
  inherited forever after.

## How it improves over time

Two clocks, both feeding the same file:

1. **Correction loop (Phase 1b).** On each run the bot reads back what you actually set in Firefly and
   appends `corrected` records (`source="user"`). A correction is gold: it fixes the wrong example *and*
   sharpens the boundary around it.
2. **Density loop.** More labelled examples → denser embedding neighbourhoods → higher `top_sim` →
   fewer transactions fall below the gate → less review next time. The system asymptotically does more
   on its own.

Plus two periodic effects:
- **Re-clustering** of the growing orphan pile crystallises new categories you didn't think to define.
- **Active learning:** the bot can rank `needs-review` items by *impact* (how many transactions a
  decision would settle) × *uncertainty*, so the handful you review first buys the most accuracy. Early
  corrections have outsized leverage precisely because data is sparse.

Honest limitations: zero-shot label-name matching needs calibration (small margins); heuristic cluster
naming is crude until the Phase 4 LLM improves it; and weak self-training (treating high-confidence
auto-labels as truth) can propagate errors — so new labels always require your one-click confirmation
before they anchor others.

## How YOU can boost the cold start (highest leverage first)

1. **Bootstrap from your existing Firefly history.** Even two or three months of transactions you've
   *already categorised* is a ready-made labelled dataset in your exact domain. A one-off, read-only
   importer can export it into `labels.jsonl` so Phase 2 is warm on the first real run. This is the
   single biggest lever — it converts "cold" into "lukewarm" instantly.
2. **Confirm a starter taxonomy.** Give (or confirm) the category names you actually want to use in
   Dutch. Zero-shot (Step 3b) works from day one off *names alone*, before any examples exist — so a
   good name list is immediately productive.
3. **Do the first review pass — but only the items it flags.** Spend ten minutes on the `needs-review`
   list, ideally in the bot's suggested order (high-impact, high-uncertainty first). Each correction
   labels a whole merchant/cluster, not just one row.
4. **Pre-label your top recurring merchants.** The 80/20: confirm the dozen merchants you transact with
   most and you've covered the majority of future volume.
5. **Import more history if you have it.** More months = more examples = the simplest accuracy lever.
   Re-imports are safe (Firefly's duplicate-hash detection).
6. **Trust, then spot-check, the MCC labels.** Card spend is auto-labelled from MCC on day one; glance
   at a few to confirm the MCC→category map matches your naming, and adjust the map once.

The throughline: you are never *training* in a way that demands a big dataset. You seed names and a few
corrections; MCC and merchant-consistency do the heavy lifting; and every run + correction compounds —
on a file you own and can read.
