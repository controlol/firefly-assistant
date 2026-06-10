# Validation samples

Drop real invoice/receipt files here to validate OCR extraction:

```
samples/invoices/      ← your .pdf / .png / .jpg files go here (gitignored)
samples/expected.json  ← optional ground truth for a pass/fail check (gitignored)
```

**These files are gitignored on purpose** — real invoices contain IBANs, amounts and personal
data. Only this README and `invoices/.gitkeep` are tracked. Never commit actual documents.

## Run the validation harness

```bash
# CPU is fine for a handful of documents; add --gpu if PaddleOCR has a working GPU build.
python scripts/validate_ocr.py --show-text
```

It runs the real PaddleOCR recogniser + the Dutch heuristics over every file in
`samples/invoices/` and prints the extracted total, IBAN and confidences. `--show-text` also
dumps the raw OCR text so you can see what the heuristics had to work with and tune them.

## Optional: measure accuracy with ground truth

Copy `expected.example.json` to `expected.json` and fill in what each file *should* extract:

```json
{
  "acme-hosting.pdf": { "total": "121.00", "iban": "NL91ABNA0417164300" },
  "albert-heijn.jpg": { "total": "23.47" }
}
```

The harness then compares extraction against it and prints PASS/FAIL per field plus an accuracy
summary — turning ad-hoc checking into a repeatable regression you can re-run as you tune.
