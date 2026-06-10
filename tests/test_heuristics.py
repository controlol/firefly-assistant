"""Unit tests for the pure Dutch-invoice heuristics (no OCR, no network)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from firefly_bot.models import FieldConfidence
from firefly_bot.ocr.heuristics import (
    extract_iban,
    extract_invoice_date,
    extract_invoice_number,
    extract_total,
    normalise_reference,
    number_from_filename,
)

SAMPLE = """\
Acme Webhosting B.V.
Kerkstraat 1, 1234 AB Amsterdam
IBAN: NL91 ABNA 0417 1643 00
Omschrijving                Bedrag
Hosting pakket Pro          € 100,00
BTW 21%                     € 21,00
Totaal te betalen           € 121,00
"""


def test_extract_iban_normalises_and_is_high_confidence() -> None:
    iban, conf = extract_iban(SAMPLE)
    assert iban == "NL91ABNA0417164300"
    assert conf is FieldConfidence.HIGH


def test_extract_total_prefers_strong_keyword_line() -> None:
    total, conf = extract_total(SAMPLE)
    assert total == Decimal("121.00")
    assert conf is FieldConfidence.HIGH


def test_extract_total_handles_thousands_separator() -> None:
    total, _ = extract_total("Totaal te betalen   € 1.234,56")
    assert total == Decimal("1234.56")


def test_no_amount_yields_none() -> None:
    total, conf = extract_total("geen bedragen hier")
    assert total is None
    assert conf is FieldConfidence.NONE


# --- patterns observed in real OCR output --------------------------------------------------

def test_label_and_amount_glued_together() -> None:
    # OCR often drops spaces: "Totaal incl. BTW 1.562,50" -> "Totaalincl.BTW1.562,50".
    total, conf = extract_total("Netto 1.291,32\nTotaalincl.BTW1.562,50\nBTW 21% 271,18")
    assert total == Decimal("1562.50")
    assert conf is FieldConfidence.HIGH


def test_label_and_amount_on_separate_lines() -> None:
    text = "Totaal\nTe betalen\n28,51\n0,00\n28,51"
    total, conf = extract_total(text)
    assert total == Decimal("28.51")
    assert conf is FieldConfidence.HIGH


def test_excl_plus_vat_reconciliation_finds_unlabelled_grand_total() -> None:
    # The grand total 1.772,00 has no label of its own; excl 1.464,46 + BTW 307,54 == 1.772,00.
    text = (
        "Totaal exclusief BTW:\n1.464,46\n"
        "21,00% BTW over 1.464,46\n307,54\n"
        "1.772,00"
    )
    total, conf = extract_total(text)
    assert total == Decimal("1772.00")
    assert conf is FieldConfidence.HIGH


def test_excl_btw_subtotal_is_not_taken_as_total() -> None:
    # A "Totaal exclusief BTW" line must not be returned when an inclusive total exists.
    text = "Totaal exclusief BTW 1.000,00\nTe betalen\n1.210,00"
    total, _ = extract_total(text)
    assert total == Decimal("1210.00")


# --- invoice number & date -----------------------------------------------------------------

def test_invoice_number_same_line() -> None:
    number, conf = extract_invoice_number("Factuurnummer F26000352\nKlantnummer R00002921")
    assert number == "F26000352"
    assert conf is FieldConfidence.HIGH


def test_invoice_number_does_not_grab_customer_number() -> None:
    # "Klantnummer" is not an invoice-number label.
    number, _ = extract_invoice_number("Klantnummer R00002921\nFactuurnummer 2026-4542")
    assert number == "2026-4542"


def test_invoice_number_from_filename_fallback() -> None:
    number, conf = number_from_filename("Factuur - F26000352.pdf")
    assert number == "F26000352"
    assert conf is FieldConfidence.MEDIUM


def test_normalise_reference_matches_across_formatting() -> None:
    assert normalise_reference("2026-4542") in normalise_reference("betaling 2026 4542 nota")


def test_invoice_date_numeric_dutch_format() -> None:
    d, conf = extract_invoice_date("Factuurdatum\n12-5-2026")
    assert d == date(2026, 5, 12)
    assert conf is FieldConfidence.HIGH


def test_invoice_date_month_name() -> None:
    d, _ = extract_invoice_date("Factuurdatum: 28 april 2026")
    assert d == date(2026, 4, 28)


def test_invoice_date_absent_is_none() -> None:
    d, conf = extract_invoice_date("geen datum hier")
    assert d is None
    assert conf is FieldConfidence.NONE
