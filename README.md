# firefly-bot

A self-hosted, **local-first** Python tool for personal [Firefly III](https://www.firefly-iii.org/)
finances. It does two jobs:

1. **Documents → transactions.** Ingest invoices/receipts from an email inbox, OCR the key fields
   locally, **match** each document to an existing Firefly bank transaction, **attach** the document,
   tag it, and emit an `.xlsx` audit report with deep links back into Firefly.
2. **Bank statements → enriched transactions.** Import CAMT.053 statements, reconcile balances, and
   **categorize** each transaction with a local embedding model that **learns from your corrections**
   over time — no cloud, no data leaves your machine.

Everything that crosses a module boundary is a typed pydantic v2 model; `mypy --strict` is enforced.
Financial data (the label store, statements, invoices) stays local and is gitignored.

See [PLAN.md](./PLAN.md) for the build-vs-buy rationale, [docs/ENRICHMENT.md](./docs/ENRICHMENT.md)
for the enrichment roadmap, and [docs/COLD_START.md](./docs/COLD_START.md) for how the bot learns
from little data.

## What it does

- **Email → OCR → match → attach.** IMAP ingest, local OCR (RapidOCR), Dutch invoice heuristics
  (total, IBAN, invoice number, date), confidence-scored matching, auto-attach + tag, audit report.
- **UBL e-invoices as source of truth.** When an email carries a UBL/NLCIUS invoice it is parsed
  structurally instead of OCR'd, and the human-readable PDF embedded inside the UBL is extracted and
  attached.
- **CAMT.053 import** with opening/closing **balance reconciliation**, transfer detection for your own
  accounts, and opposing-account de-duplication.
- **Local embedding enrichment.** On import, each transaction is categorized via a cascade —
  **MCC → k-NN over your labelled history → zero-shot against category names → confidence gate** —
  applying confident categories automatically and routing the rest to `needs-review`.
- **A learning loop you own.** Every decision is appended to `data/labels.jsonl`; `bootstrap` warms
  it from your Firefly history, and `reconcile-labels` turns the corrections you make in Firefly into
  new training examples. It gets better the more you use it.
- **New-label discovery.** Recurring transactions the bot can't categorize are clustered into
  candidate *new* categories you haven't defined yet, surfaced for one-click accept.

## Pipelines

```
Documents:  inbox (IMAP) ─▶ ingest ─▶ UBL parse / OCR + Dutch heuristics ─▶ match ─▶ attach + tag ─▶ report (.xlsx)
Statements: CAMT.053 file ─▶ parse + reconcile balances ─▶ categorise (MCC▸k-NN▸zero-shot▸gate) ─▶ create txns (+category/tags) ─▶ labels.jsonl
Learning:   bootstrap (Firefly history) ─▶ labels.jsonl ─▶ import categorises ─▶ you fix in Firefly ─▶ reconcile-labels ─▶ better next run
```

## Quick start

Uses [uv](https://docs.astral.sh/uv/) as the package manager. Python **3.14+**.

```bash
uv venv
uv pip install -e ".[dev]"
cp .env.example .env                    # then edit (Firefly URL + token, IMAP, BANK_OWNER_NAME)
uv run firefly-bot run --dry-run -v     # read + match + report, write nothing — do this first
uv run firefly-bot run -v               # live: attach documents + tag transactions
```

`--dry-run` reads your real data and produces the audit report but suppresses every write to Firefly
(via `DryRunLedger`) and every write to the label store (via `NullLabelStore`). Use it first.

## Commands

| Command | What it does |
|---|---|
| `firefly-bot run` | Ingest documents (IMAP or `--source folder`), match, attach, tag, report. |
| `firefly-bot import --camt FILE` | Import a CAMT.053 statement; reconcile balances; categorise. |
| `firefly-bot bootstrap` | **Read-only** pass over Firefly history → seed `labels.jsonl` (cold-start booster). |
| `firefly-bot reconcile-labels` | Read your category corrections in Firefly → append `corrected` examples. |

All accept `--dry-run`, `-v`, and `--profile NAME` (overlay `.env.NAME` on `.env`). `import` takes
`--create-account` (create the asset account if the statement IBAN matches none). `bootstrap`/
`reconcile-labels` take `--days N` (default 365) or `--since YYYY-MM-DD`.

To test the document path against local files instead of an inbox (no IMAP needed):

```bash
uv run firefly-bot run --dry-run -v --source folder --folder samples/invoices
```

## Enrichment & the learning loop

Enrichment is **local and private** — a small multilingual embedding model (`intfloat/multilingual-e5-small`
via [fastembed](https://github.com/qdrant/fastembed), **no torch**, reusing the project's existing
ONNXRuntime) runs on CPU in a few milliseconds per transaction. The model downloads once on first use;
nothing is sent anywhere.

It is designed to be useful immediately, even on a couple of months of freshly imported data, because
three signals need **zero training**: card **MCC** codes, **recurring merchants**, and category **names**
themselves (matched zero-shot). Quick start for a warm first run:

```bash
uv run firefly-bot bootstrap            # seed labels.jsonl from your categorised Firefly history
uv run firefly-bot import --camt statement.xml   # transactions get categorised; weak ones tagged needs-review
# ...review/fix the needs-review ones in Firefly, then:
uv run firefly-bot reconcile-labels     # your fixes become training examples → better next time
```

The classify rule, learned in build and tuned for cold start (a *confident* k-NN wins; otherwise a
gated zero-shot label name; otherwise a weak k-NN is only a suggestion routed to review), plus the
new-label discovery and cold-start boosters, are documented in
[docs/COLD_START.md](./docs/COLD_START.md). Configure thresholds via `ENRICH_*` env vars (see
`EnrichSettings`). Set `ENRICH_ENABLED=false` for plain MCC-only categorisation (no model load).

See the enrichment in action with the bundled demos (real model, no Firefly needed):

```bash
uv run python experiments/enrich_demo.py        # the cascade + a one-correction density-loop
uv run python experiments/discovery_demo.py     # discovering categories you never defined
```

## Bank import (CAMT.053)

```bash
uv run firefly-bot import --camt statement.xml --dry-run        # parse + plan, write nothing
uv run firefly-bot import --camt statement.xml                  # import into the matching account
uv run firefly-bot import --camt statement.xml --create-account # also create the asset account if missing
```

The bot finds the asset account to import into by **matching the statement IBAN** against your
existing Firefly asset accounts. By default, if no account matches it **stops with an error** rather
than create one — this avoids a silent duplicate when an existing account simply has no IBAN set (a
common cause). Either set the IBAN on your existing account and re-run, or pass `--create-account` to
let the import create it (named `<BANK_ACCOUNT_NAME> <last 6 of IBAN>`). On a brand-new Firefly with
no account yet, use `--create-account` for the first import.

Set `BANK_OWNER_NAME` in `.env` so movements to your own accounts (savings) import as **transfers**,
not income/expense. Re-imports are safe — Firefly's duplicate-hash detection skips transactions that
already exist. Opposing accounts are de-duplicated (IBAN, then normalised name, then fuzzy match) to
avoid "Albert Heijn 2264/2277" proliferation; opening/closing balances are **reconciled** and a
mismatch is reported (never fatal). Embedding-based merchant merging is available but **off by
default** — see Findings.

## Email ingestion

Point it at a dedicated IMAP mailbox that receives invoice emails (default `--source imap`). Each run
processes the messages in the inbox; a message is **moved to the `Processed` folder only after its
invoice is attached** — so an invoice that arrives before its bank transaction (or one with a human
error) stays in the inbox and is retried next run. Emails that arrive **without a usable attachment**
are **starred** for manual review. **Emails are never deleted.**

Matching uses the **invoice number** (found in the transaction reference), **amount**, IBAN, and
**invoice date**. Auto-attach requires the invoice number to appear in the transaction plus a
corroborating amount/IBAN; weaker matches are attached with the `needs-review` tag.

## OCR engine

The default engine is **RapidOCR** (PP-OCR models on ONNXRuntime): CPU-friendly, no paddlepaddle,
models ship in the wheel. PaddleOCR is available behind the `paddle` extra but is not recommended —
paddle 3.x currently fails the PP-OCRv3 graphs on CPU/Windows.

```bash
# Validate OCR on real invoices: drop .pdf/.png/.jpg in samples/invoices/ (gitignored), then:
uv run python scripts/validate_ocr.py --show-text   # optional samples/expected.json scores accuracy
```

## Design: injected boundaries

The orchestration depends on `Protocol`s, each with a real default and a fake for tests, so the whole
pipeline — including the auto-write and categorisation paths — is unit-testable without a live inbox,
Firefly, OCR, or embedding model, and `--dry-run` is a one-line injection:

- `AttachmentSource` (IMAP / folder), `TextRecogniser` (RapidOCR / Paddle / future vision-LLM)
- `Ledger` + `StatementWriter` (Firefly write surfaces; `DryRunLedger` suppresses writes)
- `ReportWriter` (.xlsx audit), `LabelStore` (`JsonlLabelStore` / `NullLabelStore`)
- `Embedder` (`E5Embedder` / deterministic `FakeEmbedder`) behind the `Categoriser`

## Develop

```bash
uv run pytest         # unit tests — no network, OCR, or embedding model needed (deterministic fakes)
uv run mypy src       # strict type-check
uv run ruff check src # lint
```

## Status

End-to-end and tested (**97 tests**, `mypy --strict` + `ruff` clean). Complete:

- **Documents:** IMAP/folder ingest, UBL parse + embedded-PDF extraction, RapidOCR, Dutch heuristics,
  confidence matching, attach + tag, xlsx report.
- **Statements:** CAMT.053 parse + **balance reconciliation**, transfer detection, opposing-account
  resolution, import.
- **Enrichment (Phase 2):** embedding categoriser (MCC → k-NN → zero-shot → gate), new-label
  discovery, history **bootstrap**, and **correction capture** — the full learning loop.

**Future (data prerequisites now in place):**

- **Phase 3 — learned match scorer:** replace the additive matching heuristic with a small local model
  trained on captured match negatives + corrections.
- **Phase 4 — recurring detection + local LLM** (via Ollama) for the residual hard cases, fully offline.

## Findings & decisions

- **pycamt rejected.** A third-party CAMT decoder could not parse the Dutch `.02` dialect and dropped
  fields we depend on; the hand-rolled `xml.etree` parser is more robust. Long-run stability is pursued
  via XSD validation + golden-file tests per bank instead.
- **Local embeddings, not an LLM, not a cloud API.** fastembed + e5-small is private, ~4 ms/transaction,
  and adds no torch. Classification is k-NN/zero-shot (no training); only Phase 3 fits a tiny model.
- **Merchant-merging stays off by default.** A probe showed e5 cannot separate Dutch merchant *variants*
  from *distinct* merchants (they overlap), and a wrong merge corrupts the ledger — so embedding-based
  merchant resolution is opt-in behind a high gate (`ENRICH_MERCHANT_RESOLUTION`).
