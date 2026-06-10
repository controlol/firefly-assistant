"""Validate OCR extraction against real invoice/receipt files.

Runs the real PaddleOCR recogniser + Dutch heuristics over every document in a directory and
reports the extracted total / IBAN / confidence. If a ground-truth JSON is present, also prints
PASS/FAIL per field and an accuracy summary.

    python scripts/validate_ocr.py --show-text
    python scripts/validate_ocr.py --dir samples/invoices --expected samples/expected.json --gpu

Runnable without installing the package (it adds ./src to sys.path).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from firefly_bot.models import Attachment, ExtractedInvoice  # noqa: E402
from firefly_bot.ocr.extract import (  # noqa: E402
    PaddleTextRecogniser,
    RapidOcrTextRecogniser,
    TextRecogniser,
    extract_invoice,
)

_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _load_attachment(path: Path) -> Attachment:
    data = path.read_bytes()
    content_type = _CONTENT_TYPES.get(path.suffix.lower())
    if content_type is None:
        raise ValueError(f"Unsupported file type: {path.name}")
    return Attachment(
        filename=path.name,
        content_type=content_type,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        source_message_id="local-validation",
        received_at=datetime.now(timezone.utc),
    )


def _check(inv: ExtractedInvoice, expected: dict[str, str]) -> tuple[bool, str]:
    """Compare extraction to ground truth. Returns (all_ok, message)."""
    notes: list[str] = []
    ok = True

    if "total" in expected:
        try:
            want = Decimal(expected["total"])
        except InvalidOperation:
            return False, f"bad expected total {expected['total']!r}"
        got = inv.total_amount
        passed = got is not None and got == want
        ok &= passed
        notes.append(f"total {'OK' if passed else f'FAIL (want {want}, got {got})'}")

    if "iban" in expected:
        want_iban = expected["iban"].replace(" ", "").upper()
        passed = inv.counterparty_iban == want_iban
        ok &= passed
        notes.append(
            f"iban {'OK' if passed else f'FAIL (want {want_iban}, got {inv.counterparty_iban})'}"
        )

    return ok, "; ".join(notes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate OCR extraction on sample invoices.")
    parser.add_argument("--dir", default="samples/invoices", help="Directory of documents.")
    parser.add_argument("--expected", default="samples/expected.json", help="Ground-truth JSON.")
    parser.add_argument(
        "--engine", choices=["rapid", "paddle"], default="rapid", help="OCR engine."
    )
    parser.add_argument("--dpi", type=int, default=200, help="Rasterisation DPI for PDFs.")
    parser.add_argument("--lang", default="nl", help="Language model (paddle engine only).")
    parser.add_argument("--show-text", action="store_true", help="Dump raw OCR text per file.")
    args = parser.parse_args(argv)

    files = sorted(
        p for p in Path(args.dir).iterdir()
        if p.is_file() and p.suffix.lower() in _CONTENT_TYPES
    )
    if not files:
        print(f"No documents found in {args.dir}/. Drop some .pdf/.png/.jpg files there.")
        return 1

    expected_all: dict[str, dict[str, str]] = {}
    expected_path = Path(args.expected)
    if expected_path.exists():
        expected_all = json.loads(expected_path.read_text(encoding="utf-8"))

    print(f"Loading OCR engine '{args.engine}' (dpi={args.dpi})...")
    recogniser: TextRecogniser = (
        RapidOcrTextRecogniser(dpi=args.dpi)
        if args.engine == "rapid"
        else PaddleTextRecogniser(lang=args.lang, dpi=args.dpi)
    )

    checked = passed = 0
    for path in files:
        inv = extract_invoice(_load_attachment(path), recogniser)
        print(f"\n=== {path.name} ===")
        print(f"  total : {inv.total_amount}  ({inv.total_confidence.name})")
        print(f"  iban  : {inv.counterparty_iban}  ({inv.iban_confidence.name})")
        if args.show_text:
            print("  --- raw OCR text ---")
            print("\n".join(f"    {line}" for line in inv.raw_text.splitlines()))

        if path.name in expected_all:
            ok, message = _check(inv, expected_all[path.name])
            checked += 1
            passed += int(ok)
            print(f"  check : {'PASS' if ok else 'FAIL'} — {message}")

    if checked:
        print(f"\nAccuracy: {passed}/{checked} files fully correct.")
    else:
        print("\nNo ground truth matched — add samples/expected.json to measure accuracy.")
    return 0 if checked == passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
