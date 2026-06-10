"""Test fixtures for the Firefly III instance: seed a CAMT.053 statement and reset it again.

    uv run python scripts/firefly_fixture.py seed --camt samples/<file>.xml --owner-name "..."
    uv run python scripts/firefly_fixture.py reset

`seed` delegates to the product importer (`firefly_bot.banking`), tagging everything it creates
`bot-fixture`, then makes the accounts visible on the dashboard and records the created account
ids in a local state file. `reset` deletes every `bot-fixture` transaction, those accounts, and
all expense/revenue (opposing) accounts, restoring a clean state.

Workflow: run `reset` then `seed` once at the START of a test session. The data is meant to
persist between individual tests — do not reset between them. TEST INSTANCE ONLY.

Reads FIREFLY_BASE_URL / FIREFLY_TOKEN from .env (never printed).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from firefly_bot.banking.camt import parse_camt053  # noqa: E402
from firefly_bot.banking.importer import import_statement  # noqa: E402
from firefly_bot.config import FireflySettings  # noqa: E402
from firefly_bot.firefly.client import FireflyClient  # noqa: E402

_FIXTURE_TAG = "bot-fixture"
_ACCOUNT_MARKER = "[bot-fixture]"  # legacy asset-account naming; still cleaned up by reset
_STATE_FILE = _ROOT / ".fixture-state.json"


def _raw_client(settings: FireflySettings) -> httpx.Client:
    """A plain httpx client for the test-only operations the product client doesn't expose."""
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


def seed(camt_path: Path, owner_name: str | None = None) -> None:
    statement = parse_camt053(camt_path)
    print(f"Parsed {len(statement.transactions)} entries for {statement.account_iban}.")
    settings = FireflySettings()
    with FireflyClient(settings) as client:
        summary = import_statement(
            statement,
            client,
            owner_name=owner_name,
            account_name="Betaalrekening",
            extra_tags=(_FIXTURE_TAG,),
            skip_duplicates=True,
        )
    account_ids = [summary.asset_account_id, *summary.savings_account_ids.values()]
    with _raw_client(settings) as client:
        _show_on_dashboard(client, account_ids)
    _write_state(account_ids)
    print(
        f"Created {summary.created}/{summary.total} transactions "
        f"({summary.transfers} transfers, {summary.duplicates} duplicates, "
        f"{summary.errors} errors). Accounts on dashboard: {account_ids}"
    )


def reset() -> None:
    """Delete everything the fixtures created. TEST INSTANCE ONLY."""
    settings = FireflySettings()
    state_ids = set(_read_state())
    with _raw_client(settings) as client:
        transaction_ids = _fixture_transaction_ids(client)
        for tid in transaction_ids:
            client.delete(f"/api/v1/transactions/{tid}").raise_for_status()

        deleted_assets: list[str] = []
        for account in _all_accounts(client, "asset"):
            attrs = account["attributes"]  # type: ignore[index]
            if _ACCOUNT_MARKER in attrs["name"] or str(account["id"]) in state_ids:
                client.delete(f"/api/v1/accounts/{account['id']}").raise_for_status()
                deleted_assets.append(str(account["id"]))
        _remove_from_dashboard(client, deleted_assets)

        removed_opposing = 0
        for account_type in ("expense", "revenue"):
            for account in _all_accounts(client, account_type):
                client.delete(f"/api/v1/accounts/{account['id']}").raise_for_status()
                removed_opposing += 1

        print(
            f"Deleted {len(transaction_ids)} {_FIXTURE_TAG} transactions, "
            f"{len(deleted_assets)} asset + {removed_opposing} opposing account(s)."
        )
    _clear_state()


# --- state file (which asset accounts the last seed created) -------------------------------

def _write_state(asset_account_ids: list[str]) -> None:
    _STATE_FILE.write_text(json.dumps({"asset_account_ids": asset_account_ids}), encoding="utf-8")


def _read_state() -> list[str]:
    if not _STATE_FILE.exists():
        return []
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        return [str(x) for x in data.get("asset_account_ids", [])]
    except (json.JSONDecodeError, OSError):
        return []


def _clear_state() -> None:
    _STATE_FILE.unlink(missing_ok=True)


# --- dashboard + lookup helpers ------------------------------------------------------------

def _show_on_dashboard(client: httpx.Client, account_ids: list[str]) -> None:
    try:
        current = client.get("/api/v1/preferences/frontpageAccounts")
        existing = (
            current.json()["data"]["attributes"]["data"] if current.status_code == 200 else []
        )
        merged = list(dict.fromkeys([str(x) for x in existing] + account_ids))
        client.put(
            "/api/v1/preferences/frontpageAccounts", json={"data": merged}
        ).raise_for_status()
        print(f"  Dashboard accounts: {merged}")
    except (httpx.HTTPError, KeyError, TypeError) as exc:  # best effort
        print(f"  (could not update dashboard preference: {exc})")


def _remove_from_dashboard(client: httpx.Client, account_ids: list[str]) -> None:
    if not account_ids:
        return
    try:
        current = client.get("/api/v1/preferences/frontpageAccounts")
        existing = (
            current.json()["data"]["attributes"]["data"] if current.status_code == 200 else []
        )
        drop = set(account_ids)
        remaining = [str(x) for x in existing if str(x) not in drop]
        client.put(
            "/api/v1/preferences/frontpageAccounts", json={"data": remaining}
        ).raise_for_status()
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
        if page >= int(pagination.get("total_pages", page) or 1):
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
    sub.add_parser("reset", help="Delete bot-fixture transactions and the fixture accounts.")
    args = parser.parse_args(argv)

    if args.command == "seed":
        seed(Path(args.camt), owner_name=args.owner_name)
    elif args.command == "reset":
        reset()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
