"""Parse CAMT.053 bank statements into typed models.

Pure parsing — no Firefly I/O — so it is trivially unit-testable. The namespace is read from the
document root, so any camt.053.001.xx version works (the Dutch banks emit .02).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class BankTransaction(BaseModel):
    """One statement entry, normalised to a positive amount + direction."""

    model_config = ConfigDict(frozen=True)

    date: str  # ISO date (YYYY-MM-DD) as it appears in the statement
    amount: Decimal  # always positive
    is_outgoing: bool  # True = money left the account (debit / withdrawal)
    description: str
    counterparty_name: str
    counterparty_iban: str | None = None
    reference: str | None = None  # bank reference, used as external_id for dedup/traceability


class BankStatement(BaseModel):
    account_iban: str
    currency: str
    transactions: list[BankTransaction]


def parse_camt053(source: Path | bytes | str) -> BankStatement:
    root = _root(source)
    ns = {"c": root.tag.split("}")[0].strip("{")}
    stmt = root.find(".//c:Stmt", ns)
    if stmt is None:
        raise ValueError("No <Stmt> element found — is this a CAMT.053 statement?")

    account_iban = stmt.findtext("c:Acct/c:Id/c:IBAN", namespaces=ns) or ""
    currency = stmt.findtext("c:Acct/c:Ccy", namespaces=ns) or "EUR"

    transactions: list[BankTransaction] = []
    for ntry in stmt.findall("c:Ntry", ns):
        amount = Decimal(ntry.findtext("c:Amt", default="0", namespaces=ns))
        if amount == 0:
            continue
        indicator = (ntry.findtext("c:CdtDbtInd", namespaces=ns) or "DBIT").upper()
        reversed_ = (ntry.findtext("c:RvslInd", namespaces=ns) or "false").lower() == "true"
        is_outgoing = (indicator == "DBIT") != reversed_  # a reversal flips the direction
        date = (
            ntry.findtext("c:BookgDt/c:Dt", namespaces=ns)
            or ntry.findtext("c:ValDt/c:Dt", namespaces=ns)
            or ""
        )
        tx = ntry.find("c:NtryDtls/c:TxDtls", ns)
        name, iban, remit = _counterparty(tx, ns)
        additional = ntry.findtext("c:AddtlNtryInf", namespaces=ns)
        if not name:
            # Card/POS payments carry the merchant in AddtlNtryInf as "<name> > <location> ...".
            name = _name_from_additional(additional)
        description = remit or additional or name or "Transactie"
        reference = _reference(tx, ns) or ntry.findtext("c:NtryRef", namespaces=ns)
        transactions.append(
            BankTransaction(
                date=date,
                amount=amount,
                is_outgoing=is_outgoing,
                description=description.strip()[:255],
                counterparty_name=(name or "Onbekend").strip()[:255],
                counterparty_iban=iban,
                reference=(reference or None),
            )
        )
    return BankStatement(account_iban=account_iban, currency=currency, transactions=transactions)


def _root(source: Path | bytes | str) -> ET.Element:
    if isinstance(source, Path):
        return ET.parse(source).getroot()
    if isinstance(source, bytes):
        return ET.fromstring(source)
    return ET.fromstring(source)


def _counterparty(
    tx: ET.Element | None, ns: dict[str, str]
) -> tuple[str | None, str | None, str | None]:
    if tx is None:
        return None, None, None
    related = tx.find("c:RltdPties", ns)
    name = iban = None
    if related is not None:
        name = related.findtext("c:Cdtr/c:Nm", namespaces=ns) or related.findtext(
            "c:Dbtr/c:Nm", namespaces=ns
        )
        iban = related.findtext("c:CdtrAcct/c:Id/c:IBAN", namespaces=ns) or related.findtext(
            "c:DbtrAcct/c:Id/c:IBAN", namespaces=ns
        )
    remit = " ".join(e.text or "" for e in tx.findall("c:RmtInf/c:Ustrd", ns)).strip()
    return name, iban, (remit or None)


def _reference(tx: ET.Element | None, ns: dict[str, str]) -> str | None:
    if tx is None:
        return None
    refs = tx.find("c:Refs", ns)
    if refs is None:
        return None
    return refs.findtext("c:AcctSvcrRef", namespaces=ns) or refs.findtext(
        "c:TxId", namespaces=ns
    )


def _name_from_additional(additional: str | None) -> str | None:
    """Merchant name from ASN/RegioBank card text: '<name>  > <location> <date> ...'."""
    if not additional or ">" not in additional:
        return None
    head = re.sub(r"\s{2,}", " ", additional.split(">", 1)[0]).strip()
    return head or None
