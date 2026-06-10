"""Parse UBL e-invoices (OASIS UBL 2.1 Invoice/CreditNote, incl. the Dutch NLCIUS profile).

A UBL XML is a structured source of truth — invoice number, date, total, supplier and IBAN are
explicit fields, so extraction is exact and HIGH-confidence (no OCR). `parse_ubl` returns None
for anything that isn't a UBL invoice, so the caller can fall back to OCR.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal, InvalidOperation

from firefly_bot.models import Attachment, ExtractedInvoice, FieldConfidence

XML_CONTENT_TYPES = frozenset({"application/xml", "text/xml"})

_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
_CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
_NS = {"cbc": _CBC, "cac": _CAC}
_UBL_ROOTS = frozenset({"Invoice", "CreditNote"})


def is_xml(attachment: Attachment) -> bool:
    return (
        attachment.content_type in XML_CONTENT_TYPES
        or attachment.filename.lower().endswith(".xml")
    )


def is_ubl_document(attachment: Attachment) -> bool:
    """Cheap check: an XML attachment whose root is a UBL Invoice/CreditNote."""
    if not is_xml(attachment):
        return False
    try:
        root = ET.fromstring(attachment.data)
    except ET.ParseError:
        return False
    return root.tag.split("}")[-1] in _UBL_ROOTS


def embedded_pdf(attachment: Attachment) -> Attachment | None:
    """The PDF embedded in a UBL (cbc:EmbeddedDocumentBinaryObject), if present.

    NLCIUS invoices (e.g. AFAS) carry the human-readable PDF inside the XML as base64, so we can
    attach a real PDF even when the email only carried the UBL.
    """
    try:
        root = ET.fromstring(attachment.data)
    except ET.ParseError:
        return None
    for node in root.iter():
        if node.tag.split("}")[-1] != "EmbeddedDocumentBinaryObject":
            continue
        if (node.get("mimeCode") or "").lower() != "application/pdf" or not node.text:
            continue
        try:
            data = base64.b64decode(node.text, validate=False)
        except (binascii.Error, ValueError):
            continue
        stem = attachment.filename.rsplit(".", 1)[0]
        return Attachment(
            filename=node.get("filename") or f"{stem}.pdf",
            content_type="application/pdf",
            data=data,
            sha256=hashlib.sha256(data).hexdigest(),
            source_message_id=attachment.source_message_id,
            received_at=attachment.received_at,
            source_uid=attachment.source_uid,
        )
    return None


def parse_ubl(attachment: Attachment) -> ExtractedInvoice | None:
    try:
        root = ET.fromstring(attachment.data)
    except ET.ParseError:
        return None
    if root.tag.split("}")[-1] not in _UBL_ROOTS:
        return None

    number = (root.findtext("cbc:ID", namespaces=_NS) or "").strip() or None
    invoice_date = _parse_date(root.findtext("cbc:IssueDate", namespaces=_NS))
    currency = root.findtext("cbc:DocumentCurrencyCode", namespaces=_NS) or "EUR"
    name = root.findtext(
        "cac:AccountingSupplierParty/cac:Party/cac:PartyName/cbc:Name", namespaces=_NS
    ) or root.findtext(
        "cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName",
        namespaces=_NS,
    )
    total = _parse_amount(
        root.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=_NS)
        or root.findtext("cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", namespaces=_NS)
    )
    iban = _normalise_iban(
        root.findtext("cac:PaymentMeans/cac:PayeeFinancialAccount/cbc:ID", namespaces=_NS)
    )

    high = FieldConfidence.HIGH
    none = FieldConfidence.NONE
    return ExtractedInvoice(
        source=attachment,
        total_amount=total,
        currency=currency,
        counterparty_iban=iban,
        counterparty_name=(name.strip() if name else None),
        invoice_date=invoice_date,
        invoice_number=number,
        raw_text="",
        total_confidence=high if total is not None else none,
        iban_confidence=high if iban else none,
        number_confidence=high if number else none,
        date_confidence=high if invoice_date is not None else none,
    )


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _parse_amount(value: str | None) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(value.strip())
    except InvalidOperation:
        return None


def _normalise_iban(value: str | None) -> str | None:
    if not value:
        return None
    iban = re.sub(r"\s+", "", value).upper()
    return iban or None
