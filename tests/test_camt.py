"""Tests for the CAMT.053 parser (pure, no network)."""

from __future__ import annotations

from decimal import Decimal

from firefly_bot.banking.camt import parse_camt053

_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
 <BkToCstmrStmt><Stmt>
  <Acct><Id><IBAN>NL00BANK0123456789</IBAN></Id><Ccy>EUR</Ccy></Acct>
  <Ntry>
   <Amt Ccy="EUR">28.51</Amt><CdtDbtInd>DBIT</CdtDbtInd>
   <BookgDt><Dt>2026-04-22</Dt></BookgDt>
   <NtryDtls><TxDtls>
     <Refs><AcctSvcrRef>REF123</AcctSvcrRef></Refs>
     <RltdPties>
       <Cdtr><Nm>Example Vendor B.V.</Nm></Cdtr>
       <CdtrAcct><Id><IBAN>NL00BANK1122334455</IBAN></Id></CdtrAcct>
     </RltdPties>
     <RmtInf><Ustrd>factuur 2026-4542</Ustrd></RmtInf>
   </TxDtls></NtryDtls>
  </Ntry>
  <Ntry>
   <Amt Ccy="EUR">12.00</Amt><CdtDbtInd>DBIT</CdtDbtInd>
   <BookgDt><Dt>2026-04-23</Dt></BookgDt>
   <NtryDtls><TxDtls></TxDtls></NtryDtls>
   <AddtlNtryInf>Albert Heijn 2264     &gt;SPANBROEK 1.04.2026 MCC:5411 betaling</AddtlNtryInf>
  </Ntry>
  <Ntry>
   <Amt Ccy="EUR">100.00</Amt><CdtDbtInd>CRDT</CdtDbtInd>
   <BookgDt><Dt>2026-04-24</Dt></BookgDt>
   <NtryDtls><TxDtls>
     <RltdPties><Dbtr><Nm>Some Payer</Nm></Dbtr></RltdPties>
   </TxDtls></NtryDtls>
  </Ntry>
 </Stmt></BkToCstmrStmt>
</Document>
"""


def test_parses_account_and_entries() -> None:
    stmt = parse_camt053(_SAMPLE)
    assert stmt.account_iban == "NL00BANK0123456789"
    assert stmt.currency == "EUR"
    assert len(stmt.transactions) == 3


def test_structured_counterparty_and_remittance() -> None:
    tx = parse_camt053(_SAMPLE).transactions[0]
    assert tx.amount == Decimal("28.51")
    assert tx.is_outgoing is True
    assert tx.counterparty_name == "Example Vendor B.V."
    assert tx.counterparty_iban == "NL00BANK1122334455"
    assert tx.description == "factuur 2026-4542"
    assert tx.reference == "REF123"


def test_card_payment_name_falls_back_to_additional() -> None:
    tx = parse_camt053(_SAMPLE).transactions[1]
    assert tx.counterparty_name == "Albert Heijn 2264"
    assert tx.counterparty_iban is None
    assert tx.mcc == "5411"  # parsed from "MCC:5411"


def test_credit_entry_is_incoming() -> None:
    tx = parse_camt053(_SAMPLE).transactions[2]
    assert tx.is_outgoing is False
    assert tx.counterparty_name == "Some Payer"
