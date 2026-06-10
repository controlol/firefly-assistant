# firefly-bot

Ingest invoices/receipts from an email inbox, OCR the key fields locally, match each document
to an existing [Firefly III](https://www.firefly-iii.org/) bank transaction, attach the
document, tag it, and emit an audit report with deep links back into Firefly.

It can also import CAMT.053 bank statements directly (`firefly-bot import`), or you can use the
official [Data Importer](https://github.com/firefly-iii/data-importer) instead.

See [PLAN.md](./PLAN.md) for scope, the build-vs-buy rationale, and the roadmap.

## Pipeline

```
inbox (IMAP) → ingest → ocr.extract (PaddleOCR + Dutch heuristics) → matching → firefly (attach + tag) → report (.xlsx)
```

All inter-module data is typed (pydantic v2); `mypy --strict` is enforced.

## Quick start

Uses [uv](https://docs.astral.sh/uv/) as the package manager.

```bash
uv venv
uv pip install -e ".[dev]"
cp .env.example .env             # then edit
uv run firefly-bot run --dry-run -v   # read + match + report, write nothing — do this first
uv run firefly-bot run -v             # live: attach documents + tag transactions
```

`--dry-run` reads your real transactions and produces the audit report, but suppresses every
write to Firefly (via `DryRunLedger`). Use it to validate matching before going live.

### Import a bank statement (CAMT.053)

```bash
uv run firefly-bot import --camt statement.xml --dry-run   # parse + plan, write nothing
uv run firefly-bot import --camt statement.xml             # create transactions in Firefly
```

Set `BANK_OWNER_NAME` in `.env` so movements to your own accounts (savings) import as
**transfers**, not income/expense. Re-imports are safe — Firefly's duplicate-hash detection
skips transactions that already exist. Opposing accounts are de-duplicated (IBAN, then fuzzy
name) to avoid "Albert Heijn 2264/2277" proliferation.

To test against local documents instead of an inbox (no IMAP needed):

```bash
uv run firefly-bot run --dry-run -v --source folder --folder samples/invoices
```

Matching uses the **invoice number** (found in the transaction reference), **amount**, IBAN and
**invoice date**. Auto-attach requires the invoice number to appear in the transaction plus a
corroborating amount/IBAN; weaker matches are attached with the `needs-review` tag.

### OCR engine

The default engine is **RapidOCR** (PP-OCR models on ONNXRuntime): CPU-friendly, no
paddlepaddle, models ship in the wheel. PaddleOCR is available behind the `paddle` extra but is
not recommended — paddle 3.x currently fails the PP-OCRv3 graphs on CPU/Windows.

### Validate OCR on real invoices

```bash
# Drop .pdf/.png/.jpg files in samples/invoices/ (gitignored), then:
uv run python scripts/validate_ocr.py --show-text
```

With an optional `samples/expected.json` it prints a per-field PASS/FAIL accuracy score.

## Design: injected boundaries

`pipeline.run` depends on four `Protocol`s, each with a real default and a fake for tests:
`AttachmentSource` (ingest), `TextRecogniser` (OCR), `Ledger` (Firefly write surface), and
`ReportWriter`. This keeps the orchestration — including the auto-write path — fully unit
testable without a live inbox, Firefly, or an OCR engine, and is what makes swapping OCR
engines (RapidOCR/PaddleOCR/a future vision-LLM) and `--dry-run` one-line injections rather
than scattered conditionals.

## Develop

```bash
uv run pytest         # unit tests (no network/OCR needed)
uv run mypy src       # strict type-check
uv run ruff check src # lint
```

## Status

Iteration 1 scaffold, runnable end-to-end. Implemented: typed models, config, IMAP ingest,
PDF/image rasterisation (pypdfium2), RapidOCR recogniser (+ optional PaddleOCR), Dutch
total/IBAN heuristics, Firefly client (list/attach/tag), matcher, xlsx report, orchestration,
CLI. Validated on real Dutch invoices (3/3 total+IBAN correct at HIGH confidence). Tests cover the
pure logic and the rasterisation path (10 passing). The only piece not exercisable without
real services is the live PaddleOCR + Firefly + IMAP wiring, which needs a real inbox and a
Firefly instance. Iteration 2 (business name, VAT rows, AI categorization, recurring
detection) is described in PLAN.md.
