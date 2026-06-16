"""Tests for UBL e-invoice parsing (pure, no network)."""

from __future__ import annotations

import base64
from datetime import UTC, date, datetime
from decimal import Decimal

from firefly_bot.models import Attachment, FieldConfidence
from firefly_bot.ubl import embedded_pdf, is_ubl_document, parse_ubl

_UBL = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>V2606740</cbc:ID>
  <cbc:IssueDate>2026-04-28</cbc:IssueDate>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyName><cbc:Name>Example Vendor B.V.</cbc:Name></cac:PartyName>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:PaymentMeans>
    <cac:PayeeFinancialAccount><cbc:ID>NL00 BANK 2233445566</cbc:ID></cac:PayeeFinancialAccount>
  </cac:PaymentMeans>
  <cac:LegalMonetaryTotal>
    <cbc:TaxInclusiveAmount currencyID="EUR">1772.00</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="EUR">1772.00</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
</Invoice>
"""


def _attachment(data: bytes, filename: str = "invoice.xml") -> Attachment:
    return Attachment(
        filename=filename,
        content_type="application/xml",
        data=data,
        sha256="h",
        source_message_id="<1@x>",
        received_at=datetime(2026, 5, 1, tzinfo=UTC),
    )


def test_parses_ubl_invoice_fields() -> None:
    inv = parse_ubl(_attachment(_UBL.encode()))
    assert inv is not None
    assert inv.invoice_number == "V2606740"
    assert inv.invoice_date == date(2026, 4, 28)
    assert inv.total_amount == Decimal("1772.00")
    assert inv.counterparty_name == "Example Vendor B.V."
    assert inv.counterparty_iban == "NL00BANK2233445566"  # normalised
    assert inv.total_confidence is FieldConfidence.HIGH
    assert inv.number_confidence is FieldConfidence.HIGH


def test_non_ubl_xml_returns_none() -> None:
    assert parse_ubl(_attachment(b"<other><foo>1</foo></other>")) is None
    assert is_ubl_document(_attachment(b"<other/>")) is False


def test_is_ubl_document_true_for_ubl() -> None:
    assert is_ubl_document(_attachment(_UBL.encode())) is True


_UBL_WITH_PDF = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>X1</cbc:ID>
  <cac:AdditionalDocumentReference><cac:Attachment>
    <cbc:EmbeddedDocumentBinaryObject filename="inv.pdf"
        mimeCode="application/pdf">{b64}</cbc:EmbeddedDocumentBinaryObject>
  </cac:Attachment></cac:AdditionalDocumentReference>
</Invoice>
""".format(b64=base64.b64encode(b"%PDF-1.4 hello").decode())


def test_embedded_pdf_is_extracted() -> None:
    pdf = embedded_pdf(_attachment(_UBL_WITH_PDF.encode()))
    assert pdf is not None
    assert pdf.content_type == "application/pdf"
    assert pdf.filename == "inv.pdf"
    assert pdf.data == b"%PDF-1.4 hello"


def test_embedded_pdf_none_when_absent() -> None:
    assert embedded_pdf(_attachment(_UBL.encode())) is None
