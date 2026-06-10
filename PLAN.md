# firefly-bot — implementation plan

A self-hosted Python service that ingests invoice/receipt attachments from an email
inbox, extracts the key fields with local OCR, matches each document to an existing
Firefly III bank transaction, attaches the document and enriches the transaction, then
emits an audit summary with deep links back into Firefly.

It deliberately does **not** import bank statements itself — that is delegated to the
official [Firefly III Data Importer](https://github.com/firefly-iii/data-importer)
(CAMT.053 file import for Dutch banks, or the GoCardless feed). firefly-bot only enriches
transactions that are already in Firefly.

## Why build instead of buy (summary of research, June 2026)

The decision is binary at the platform level: **Firefly + this tool (self-hosted)** vs.
**adopt a commercial product wholesale (no Firefly)**. The research found that no product —
new or established — does email-ingest → OCR → match-to-existing-Dutch-bank-transaction for
personal finance. The market splits cleanly into two camps that never overlap:

- **Real ledger + Dutch bank feed, but no OCR/matching** (only stores receipt photos):
  PocketSmith, MoneyWiz, Lunch Money, Buxfer, Actual Budget, Firefly III.
- **Real OCR, but no Dutch bank feed / not a ledger**: Expensify, SimplyWise, Shoeboxed,
  Rolly, Yomio. Monarch does OCR + auto-link but has **zero European bank support**.

| Tool | Replaces Firefly | Dutch bank feed | OCR + auto-match | Self-host | Note |
|------|:---:|:---:|:---:|:---:|------|
| PocketSmith | ✅ | ✅ Salt Edge | ⚠️ store only | ❌ | strong forecasting |
| MoneyWiz | ✅ | ✅ Salt Edge | ⚠️ store only | ❌ | |
| Lunch Money | ✅ | ✅ PSD2 | ❌ | ❌ | good API |
| **Spendee** | ✅ (light) | ✅ Salt Edge | ⚠️ OCR **creates** txn, does **not match** feed → duplicates; no email ingest | ❌ | ~€23/yr; closest cheap consumer app but wrong model |
| Monarch | ✅ | ❌ no EU banks | ✅ | ❌ | unusable in NL |
| Moneybird / Jortt | ✅ (business) | ✅ | ✅ | ❌ | only real "buy"; business-shaped, paid |
| Expensify / SimplyWise | ❌ | ❌ | ✅ email+OCR | ❌ | US-centric, not a ledger |
| Actual Budget | ✅ | ⚠️ broken* | ❌ | ✅ | *GoCardless closed signups Jul 2025 |
| Maybe Finance | ✅ | partial | ❌ | ✅ | unmaintained (company shut 2025) |

Spendee is the closest *cheap consumer* app (bank sync + a real AI receipt scanner), but it
**creates** a transaction from a scanned photo rather than **matching** the document to the
transaction already imported from the bank feed — so it produces duplicates, has no IBAN/
counterparty reconciliation, and no email ingestion. That match-to-existing step is exactly
the gap this project fills.

- Firefly III already covers bank import, the rules engine (categorization), counterparty
  account creation, and recurring transactions natively. The only novel work is steps 1 and 4.
- **⚠️ GoCardless (PSD2 feed) closed new Bank Account Data signups in July 2025.** Existing
  users keep working; new users effectively can't get the free feed. This reinforces the
  decision to use CAMT.053 **file** import (see below) as the primary, free bank-data path.

## Iteration 1 (this scaffold) — locked scope

1. **Ingest**: poll an IMAP mailbox, pull attachments (PDF/image), dedup by content hash.
2. **Extract**: local OCR (PaddleOCR PP-Structure, two-pass) → `total` + counterparty `iban`
   (+ `date` if found). Dutch-invoice heuristics; no heavy GPU model required.
3. **Match**: find the Firefly transaction by amount (± tolerance) within a date window,
   scored by IBAN > amount+date.
4. **Act**: high confidence → auto-attach document + tag; low confidence → still attach but
   tag `needs-review`. Never silently drop.
5. **Report**: per-run `.xlsx` (and optional HTML email) with one row per document — extracted
   fields, matched transaction, confidence, action, and a direct hyperlink to the transaction.

## Iteration 2 (later)

- Business-name extraction; LLM fallback (small quantized model via Ollama) for fuzzy fields.
- VAT-per-row vs. summary VAT detection from the line-item table.
- AI categorization fallback for ambiguous cases (Firefly rules handle deterministic ones).
- Recurring-payment detection → create/link Firefly recurring transactions.
- Optional GoCardless feed wiring instead of file import.

## Architecture

```
inbox (IMAP) ──▶ ingest ──▶ ocr.extract ──▶ matching ──▶ firefly.client (attach+tag)
                                  │                              │
                              heuristics                     report.summary ──▶ .xlsx / email
```

Everything flows through typed pydantic models (see `models.py`). `mypy --strict` is enforced.

## Hardware & OCR engine

Targets CPU / a modest GPU. The engine is **RapidOCR** (PP-OCR models on ONNXRuntime): it runs
fine on CPU, ships its models in the wheel, and has no paddlepaddle dependency. PaddleOCR was
tried first but paddle 3.x fails the PP-OCRv3 graphs on CPU/Windows (OneDNN `fused_conv2d`
error), and paddle 2.6.x has no reliable Python 3.12 wheels — so PaddleOCR is kept only as an
optional `paddle` extra. The `TextRecogniser` Protocol made swapping engines a one-line change.

Iteration-1 extraction (total + IBAN) is OCR + regex/heuristics and needs no vision-LLM —
validated 3/3 correct at HIGH confidence on real Dutch invoices. Do not buy hardware until the
pipeline is proven; a GPU upgrade only pays off if iteration 2 adopts a local vision-LLM.

## Bank import decision

Start with **CAMT.053 file import** via the Data Importer (2-min monthly download, richest
data, no 90-day PSD2 re-consent, fully self-contained). Note the GoCardless feed is **no
longer available to new users** (signups closed July 2025), so file import is also the only
free option — paid aggregators (Salt Edge etc.) remain if a live feed is later required.
