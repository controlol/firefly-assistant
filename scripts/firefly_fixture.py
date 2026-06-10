"""Test fixtures for the Firefly III instance: seed transactions from a CAMT.053 file and
reset them again.

    uv run python scripts/firefly_fixture.py seed --camt samples/<file>.xml
    uv run python scripts/firefly_fixture.py reset

`seed` parses the CAMT, ensures an asset account exists, and creates one transaction per entry
(description = remittance, so invoice numbers are searchable). Everything it creates is tagged
`bot-fixture`. `reset` deletes every `bot-fixture` transaction and the fixture asset account,
restoring a clean state. Intended only for a disposable TEST Firefly instance.

Reads FIREFLY_BASE_URL / FIREFLY_TOKEN from .env (never printed).
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from firefly_bot.config import FireflySettings  # noqa: E402

_NS = {"c": "urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"}
_FIXTURE_TAG = "bot-fixture"
_ACCOUNT_MARKER = "[bot-fixture]"


@dataclass(frozen=True)
class CamtEntry:
    date: str
    amount: Decimal
    is_withdrawal: bool
    description: str
    counterparty_name: str
    counterparty_iban: str | None


def _client(settings: FireflySettings) -> httpx.Client:
    return httpx.Client(
        base_url=settings.base_url.rstrip("/"),
        headers={
            "Authorization": f"Bearer {settings.token.get_secret_value()}",
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/json",
        },
        verify=settings.verify_tls,
        timeout=30.0,
    )


# --- CAMT parsing --------------------------------------------------------------------------

def parse_camt(path: Path) -> tuple[str, str, list[CamtEntry]]:
    """Return (account_iban, currency, entries)."""
    stmt = ET.parse(path).getroot().find(".//c:Stmt", _NS)
    if stmt is None:
        raise ValueError("No <Stmt> found — is this a CAMT.053 file?")
    account_iban = stmt.findtext("c:Acct/c:Id/c:IBAN", namespaces=_NS) or ""
    currency = stmt.findtext("c:Acct/c:Ccy", namespaces=_NS) or "EUR"

    entries: list[CamtEntry] = []
    for ntry in stmt.findall("c:Ntry", _NS):
        amount = Decimal(ntry.findtext("c:Amt", default="0", namespaces=_NS))
        if amount == 0:
            continue
        indicator = (ntry.findtext("c:CdtDbtInd", namespaces=_NS) or "DBIT").upper()
        reversed_ = (ntry.findtext("c:RvslInd", namespaces=_NS) or "false").lower() == "true"
        is_withdrawal = (indicator == "DBIT") != reversed_  # reversal flips direction
        date = ntry.findtext("c:BookgDt/c:Dt", namespaces=_NS) or ntry.findtext(
            "c:ValDt/c:Dt", namespaces=_NS
        )
        tx = ntry.find("c:NtryDtls/c:TxDtls", _NS)
        name, iban, remit = _counterparty(tx)
        additional = ntry.findtext("c:AddtlNtryInf", namespaces=_NS)
        description = remit or additional or name or "Transactie"
        entries.append(
            CamtEntry(
                date=date or "",
                amount=amount,
                is_withdrawal=is_withdrawal,
                description=description.strip()[:255],
                counterparty_name=(name or "Onbekend").strip()[:255],
                counterparty_iban=iban,
            )
        )
    return account_iban, currency, entries


def _counterparty(tx: ET.Element | None) -> tuple[str | None, str | None, str | None]:
    if tx is None:
        return None, None, None
    rp = tx.find("c:RltdPties", _NS)
    name = iban = None
    if rp is not None:
        name = rp.findtext("c:Cdtr/c:Nm", namespaces=_NS) or rp.findtext(
            "c:Dbtr/c:Nm", namespaces=_NS
        )
        iban = rp.findtext("c:CdtrAcct/c:Id/c:IBAN", namespaces=_NS) or rp.findtext(
            "c:DbtrAcct/c:Id/c:IBAN", namespaces=_NS
        )
    remit = " ".join(e.text or "" for e in tx.findall("c:RmtInf/c:Ustrd", _NS)).strip()
    return name, iban, (remit or None)


# --- seed / reset --------------------------------------------------------------------------

def _ensure_asset_account(client: httpx.Client, iban: str, currency: str) -> str:
    resp = client.get("/api/v1/accounts", params={"type": "asset"})
    resp.raise_for_status()
    for account in resp.json()["data"]:
        if account["attributes"].get("iban") == iban:
            return str(account["id"])
    body = {
        "name": f"CAMT test {iban[-6:]} {_ACCOUNT_MARKER}",
        "type": "asset",
        "account_role": "defaultAsset",
        "iban": iban,
        "currency_code": currency,
    }
    created = client.post("/api/v1/accounts", json=body)
    created.raise_for_status()
    return str(created.json()["data"]["id"])


def seed(camt_path: Path) -> None:
    account_iban, currency, entries = parse_camt(camt_path)
    print(f"Parsed {len(entries)} entries for account {account_iban} ({currency}).")
    with _client(FireflySettings()) as client:
        asset_id = _ensure_asset_account(client, account_iban, currency)
        print(f"Asset account id: {asset_id}")
        created = 0
        for entry in entries:
            split: dict[str, object] = {
                "date": entry.date,
                "amount": str(entry.amount),
                "description": entry.description,
                "tags": [_FIXTURE_TAG],
            }
            if entry.is_withdrawal:
                split["type"] = "withdrawal"
                split["source_id"] = asset_id
                split["destination_name"] = entry.counterparty_name
                if entry.counterparty_iban:
                    split["destination_iban"] = entry.counterparty_iban
            else:
                split["type"] = "deposit"
                split["destination_id"] = asset_id
                split["source_name"] = entry.counterparty_name
                if entry.counterparty_iban:
                    split["source_iban"] = entry.counterparty_iban
            resp = client.post(
                "/api/v1/transactions",
                json={"apply_rules": False, "fire_webhooks": False, "transactions": [split]},
            )
            if resp.status_code >= 400:
                print(f"  ! skipped {entry.date} {entry.amount}: {resp.text[:160]}")
                continue
            created += 1
        print(f"Created {created}/{len(entries)} transactions (tagged {_FIXTURE_TAG}).")


def reset() -> None:
    with _client(FireflySettings()) as client:
        ids = _fixture_transaction_ids(client)
        for tid in ids:
            client.delete(f"/api/v1/transactions/{tid}").raise_for_status()
        print(f"Deleted {len(ids)} {_FIXTURE_TAG} transactions.")

        accounts = client.get("/api/v1/accounts", params={"type": "asset"})
        accounts.raise_for_status()
        removed = 0
        for account in accounts.json()["data"]:
            if _ACCOUNT_MARKER in account["attributes"]["name"]:
                client.delete(f"/api/v1/accounts/{account['id']}").raise_for_status()
                removed += 1
        print(f"Deleted {removed} fixture asset account(s).")


def _fixture_transaction_ids(client: httpx.Client) -> list[str]:
    ids: list[str] = []
    page = 1
    while True:
        resp = client.get("/api/v1/transactions", params={"page": page})
        resp.raise_for_status()
        body = resp.json()
        for item in body["data"]:
            splits = item["attributes"]["transactions"]
            if any(_FIXTURE_TAG in (s.get("tags") or []) for s in splits):
                ids.append(str(item["id"]))
        pagination = body.get("meta", {}).get("pagination", {})
        if page >= int(pagination.get("total_pages", page)):
            break
        page += 1
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed/reset Firefly test fixtures from CAMT.053.")
    sub = parser.add_subparsers(dest="command", required=True)
    seed_cmd = sub.add_parser("seed", help="Import CAMT.053 transactions (tagged bot-fixture).")
    seed_cmd.add_argument("--camt", required=True, help="Path to the CAMT.053 .xml file.")
    sub.add_parser("reset", help="Delete bot-fixture transactions and the fixture account.")
    args = parser.parse_args(argv)

    if args.command == "seed":
        seed(Path(args.camt))
    elif args.command == "reset":
        reset()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
