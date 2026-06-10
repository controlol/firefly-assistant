"""Test fixtures for the Firefly III instance: seed transactions from a CAMT.053 file and
reset them again.

    uv run python scripts/firefly_fixture.py seed --camt samples/<file>.xml
    uv run python scripts/firefly_fixture.py reset

`seed` parses the CAMT, ensures an asset account exists, and creates one transaction per entry
(description = remittance, so invoice numbers are searchable). Everything it creates is tagged
`bot-fixture`. `reset` deletes every `bot-fixture` transaction and the fixture asset account,
restoring a clean state. Intended only for a disposable TEST Firefly instance.

Workflow: run `reset` then `seed` once at the START of a test session. The data is meant to
persist between individual tests — do not reset between them.

Reads FIREFLY_BASE_URL / FIREFLY_TOKEN from .env (never printed).
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import httpx
from rapidfuzz import fuzz, process

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from firefly_bot.config import FireflySettings  # noqa: E402

_NS = {"c": "urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"}
_FIXTURE_TAG = "bot-fixture"
_ACCOUNT_MARKER = "[bot-fixture]"
# Opposing-account dedup: a counterparty whose normalised name scores >= this against an
# existing account is treated as the same account (reused) rather than creating a duplicate.
_FUZZY_THRESHOLD = 90
_LEGAL_SUFFIX = r"\b(b\.?v\.?|n\.?v\.?|gmbh|ltd|inc|s\.?a\.?r\.?l\.?|sa|sca|scs|cv|vof)\b"


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
        if not name:
            # Card/POS payments carry the merchant in AddtlNtryInf as "<name> > <location> ...".
            name = _name_from_additional(additional)
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


def _name_from_additional(additional: str | None) -> str | None:
    """Merchant name from ASN/RegioBank card-payment text: '<name>  > <location> <date> ...'."""
    if not additional or ">" not in additional:
        return None
    head = re.sub(r"\s{2,}", " ", additional.split(">", 1)[0]).strip()
    return head or None


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

def _normalise_name(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _ensure_account(
    client: httpx.Client, iban: str, currency: str, role: str, name_prefix: str
) -> str:
    resp = client.get("/api/v1/accounts", params={"type": "asset"})
    resp.raise_for_status()
    for account in resp.json()["data"]:
        if account["attributes"].get("iban") == iban:
            return str(account["id"])
    body = {
        "name": f"{name_prefix} {iban[-6:]} {_ACCOUNT_MARKER}",
        "type": "asset",
        "account_role": role,
        "iban": iban,
        "currency_code": currency,
    }
    created = client.post("/api/v1/accounts", json=body)
    created.raise_for_status()
    return str(created.json()["data"]["id"])


def _show_on_dashboard(client: httpx.Client, account_ids: list[str]) -> None:
    """Add the accounts to the frontpageAccounts preference so they show on the dashboard."""
    try:
        current = client.get("/api/v1/preferences/frontpageAccounts")
        existing = (
            current.json()["data"]["attributes"]["data"] if current.status_code == 200 else []
        )
        merged = list(dict.fromkeys([str(x) for x in existing] + account_ids))
        updated = client.put("/api/v1/preferences/frontpageAccounts", json={"data": merged})
        updated.raise_for_status()
        print(f"  Dashboard accounts: {merged}")
    except (httpx.HTTPError, KeyError, TypeError) as exc:  # best effort
        print(f"  (could not update dashboard preference: {exc})")


def _normalise_merchant(name: str) -> str:
    """Canonical merchant key: drop processor prefixes, store numbers, legal forms, punctuation."""
    text = name.lower().strip()
    if "*" in text:  # processor*merchant (e.g. "BCK*Vue Cinemas", "Zettle_*RUIS") -> merchant
        text = text.split("*")[-1]
    text = re.sub(_LEGAL_SUFFIX, " ", text)
    text = re.sub(r"\s+\d{2,}\b", " ", text)  # trailing store / terminal numbers
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class AccountResolver:
    """Resolves a counterparty to an existing opposing account, or signals to create one.

    Expense and revenue accounts are tracked separately because Firefly will not accept an
    expense account where a revenue account is required (and vice versa). Within a role, IBAN is
    the exact key; otherwise the normalised name is matched exactly, then fuzzily (rapidfuzz).
    Reuse avoids duplicate "Albert Heijn 2264 / 2277" accounts.
    """

    def __init__(self, client: httpx.Client) -> None:
        self._by_iban: dict[str, dict[str, str]] = {"expense": {}, "revenue": {}}
        self._by_norm: dict[str, dict[str, str]] = {"expense": {}, "revenue": {}}
        for role in ("expense", "revenue"):
            for account in _all_accounts(client, role):
                attrs = account["attributes"]  # type: ignore[index]
                self.register(attrs["name"], attrs.get("iban"), str(account["id"]), role)

    def resolve(self, name: str, iban: str | None, role: str) -> str | None:
        """Return an existing account id of this role to reuse, or None to create one."""
        by_iban, by_norm = self._by_iban[role], self._by_norm[role]
        if iban and iban in by_iban:
            return by_iban[iban]
        norm = _normalise_merchant(name)
        if not norm:
            return None
        if norm in by_norm:
            return by_norm[norm]
        if by_norm:
            match = process.extractOne(norm, list(by_norm.keys()), scorer=fuzz.token_sort_ratio)
            if match is not None and match[1] >= _FUZZY_THRESHOLD:
                return by_norm[match[0]]
        return None

    def register(self, name: str, iban: str | None, account_id: str, role: str) -> None:
        if iban:
            self._by_iban[role].setdefault(iban, account_id)
        norm = _normalise_merchant(name)
        if norm:
            self._by_norm[role].setdefault(norm, account_id)


def seed(camt_path: Path, owner_name: str | None = None) -> None:
    account_iban, currency, entries = parse_camt(camt_path)
    owner_key = _normalise_name(owner_name) if owner_name else None
    # Own accounts (e.g. savings): counterparties whose name is the account owner -> transfers.
    own_ibans = {
        e.counterparty_iban
        for e in entries
        if owner_key
        and e.counterparty_iban
        and e.counterparty_iban != account_iban
        and _normalise_name(e.counterparty_name) == owner_key
    }
    print(f"Parsed {len(entries)} entries for {account_iban} ({currency}).")
    if own_ibans:
        print(f"Detected own (savings) account(s): {', '.join(own_ibans)}")

    with _client(FireflySettings()) as client:
        asset_id = _ensure_account(client, account_iban, currency, "defaultAsset", "Betaalrekening")
        savings_ids = {
            iban: _ensure_account(client, iban, currency, "savingAsset", "Spaarrekening")
            for iban in own_ibans
        }
        _show_on_dashboard(client, [asset_id, *savings_ids.values()])
        print(f"Main account id: {asset_id}; savings: {savings_ids or 'none'}")

        resolver = AccountResolver(client)
        created = transfers = reused = 0
        for entry in entries:
            split: dict[str, object] = {
                "date": entry.date,
                "amount": str(entry.amount),
                "description": entry.description,
                "tags": [_FIXTURE_TAG],
            }
            own_side = "source" if entry.is_withdrawal else "destination"
            opposite = "destination" if entry.is_withdrawal else "source"
            if entry.counterparty_iban in savings_ids:
                own_id = savings_ids[entry.counterparty_iban]
                split["type"] = "transfer"
                src, dst = (asset_id, own_id) if entry.is_withdrawal else (own_id, asset_id)
                split["source_id"], split["destination_id"] = src, dst
                transfers += 1
            else:
                split["type"] = "withdrawal" if entry.is_withdrawal else "deposit"
                role = "expense" if entry.is_withdrawal else "revenue"
                split[f"{own_side}_id"] = asset_id
                existing_id = resolver.resolve(
                    entry.counterparty_name, entry.counterparty_iban, role
                )
                if existing_id is not None:
                    split[f"{opposite}_id"] = existing_id
                    reused += 1
                else:
                    split[f"{opposite}_name"] = entry.counterparty_name
                    if entry.counterparty_iban:
                        split[f"{opposite}_iban"] = entry.counterparty_iban
            resp = client.post(
                "/api/v1/transactions",
                json={"apply_rules": False, "fire_webhooks": False, "transactions": [split]},
            )
            if resp.status_code >= 400:
                print(f"  ! skipped {entry.date} {entry.amount}: {resp.text[:160]}")
                continue
            created += 1
            if split["type"] != "transfer":
                returned = resp.json()["data"]["attributes"]["transactions"][0]
                opp_id = returned.get(f"{opposite}_id")
                if opp_id:
                    role = "expense" if entry.is_withdrawal else "revenue"
                    resolver.register(
                        entry.counterparty_name, entry.counterparty_iban, str(opp_id), role
                    )
        print(
            f"Created {created}/{len(entries)} transactions "
            f"({transfers} transfers, {reused} reused existing accounts, tagged {_FIXTURE_TAG})."
        )


def _remove_from_dashboard(client: httpx.Client, account_ids: list[str]) -> None:
    """Drop deleted account ids from the frontpageAccounts preference."""
    if not account_ids:
        return
    try:
        current = client.get("/api/v1/preferences/frontpageAccounts")
        existing = (
            current.json()["data"]["attributes"]["data"] if current.status_code == 200 else []
        )
        drop = set(account_ids)
        remaining = [str(x) for x in existing if str(x) not in drop]
        updated = client.put("/api/v1/preferences/frontpageAccounts", json={"data": remaining})
        updated.raise_for_status()
    except (httpx.HTTPError, KeyError, TypeError):  # best effort
        pass


def _all_accounts(client: httpx.Client, account_type: str) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    page = 1
    while True:
        resp = client.get("/api/v1/accounts", params={"type": account_type, "page": page})
        resp.raise_for_status()
        body = resp.json()
        out.extend(body["data"])
        pagination = body.get("meta", {}).get("pagination", {})
        total_pages = int(pagination.get("total_pages", page))
        if total_pages == 0 or page >= total_pages:
            break
        page += 1
    return out


def reset() -> None:
    """Delete everything the fixtures created. TEST INSTANCE ONLY.

    Removes bot-fixture transactions, the [bot-fixture] asset accounts, and all expense/revenue
    (opposing) accounts — which on a disposable test instance are all auto-created by seeding.
    """
    with _client(FireflySettings()) as client:
        ids = _fixture_transaction_ids(client)
        for tid in ids:
            client.delete(f"/api/v1/transactions/{tid}").raise_for_status()
        print(f"Deleted {len(ids)} {_FIXTURE_TAG} transactions.")

        deleted_asset_ids: list[str] = []
        for account in _all_accounts(client, "asset"):
            attrs = account["attributes"]  # type: ignore[index]
            if _ACCOUNT_MARKER in attrs["name"]:
                client.delete(f"/api/v1/accounts/{account['id']}").raise_for_status()
                deleted_asset_ids.append(str(account["id"]))
        _remove_from_dashboard(client, deleted_asset_ids)
        removed_assets = len(deleted_asset_ids)

        removed_opposing = 0
        for account_type in ("expense", "revenue"):
            for account in _all_accounts(client, account_type):
                client.delete(f"/api/v1/accounts/{account['id']}").raise_for_status()
                removed_opposing += 1

        print(f"Deleted {removed_assets} fixture asset + {removed_opposing} opposing account(s).")


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
    seed_cmd.add_argument(
        "--owner-name",
        default=None,
        help="Account holder name; counterparties with this name become transfers (savings).",
    )
    sub.add_parser("reset", help="Delete bot-fixture transactions and the fixture account.")
    args = parser.parse_args(argv)

    if args.command == "seed":
        seed(Path(args.camt), owner_name=args.owner_name)
    elif args.command == "reset":
        reset()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
