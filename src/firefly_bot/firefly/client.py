"""Minimal typed Firefly III API client (only the endpoints firefly-bot needs).

Docs: https://api-docs.firefly-iii.org/ — uses a Personal Access Token (Bearer).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from firefly_bot.config import FireflySettings
from firefly_bot.models import Attachment, FireflyAccount, FireflyTransaction


class FireflyClient:
    def __init__(self, settings: FireflySettings) -> None:
        self._base_url = settings.base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {settings.token.get_secret_value()}",
                "Accept": "application/vnd.api+json",
                "Content-Type": "application/json",
            },
            verify=settings.verify_tls,
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FireflyClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def list_transactions(self, *, start: date, end: date) -> list[FireflyTransaction]:
        """Fetch withdrawals/deposits in a date window, flattened to our typed model."""
        out: list[FireflyTransaction] = []
        page = 1
        while True:
            resp = self._client.get(
                "/api/v1/transactions",
                params={"start": start.isoformat(), "end": end.isoformat(), "page": page},
            )
            resp.raise_for_status()
            body = resp.json()
            for item in body.get("data", []):
                out.extend(self._parse_transaction(item))
            pagination = body.get("meta", {}).get("pagination", {})
            if page >= int(pagination.get("total_pages", page)):
                break
            page += 1
        return out

    def _parse_transaction(self, item: dict[str, Any]) -> list[FireflyTransaction]:
        group_id = str(item["id"])
        result: list[FireflyTransaction] = []
        for split in item["attributes"]["transactions"]:
            result.append(
                FireflyTransaction(
                    id=group_id,
                    journal_id=str(split["transaction_journal_id"]),
                    date=date.fromisoformat(split["date"][:10]),
                    amount=Decimal(str(split["amount"])),
                    currency_code=split.get("currency_code", "EUR"),
                    description=split.get("description", ""),
                    source_iban=split.get("source_iban"),
                    destination_iban=split.get("destination_iban"),
                    source_name=split.get("source_name"),
                    destination_name=split.get("destination_name"),
                    tx_type=split.get("type", ""),
                    category_name=split.get("category_name"),
                    tags=tuple(split.get("tags") or ()),
                    web_url=f"{self._base_url}/transactions/show/{group_id}",
                )
            )
        return result

    def attach_document(self, transaction: FireflyTransaction, attachment: Attachment) -> str:
        """Create an attachment bound to the transaction journal and upload its bytes.

        Returns the new attachment id.
        """
        create = self._client.post(
            "/api/v1/attachments",
            json={
                "filename": attachment.filename,
                "attachable_type": "TransactionJournal",
                "attachable_id": transaction.journal_id,
            },
        )
        create.raise_for_status()
        attachment_id = str(create.json()["data"]["id"])
        upload = self._client.post(
            f"/api/v1/attachments/{attachment_id}/upload",
            content=attachment.data,
            headers={"Content-Type": "application/octet-stream"},
        )
        upload.raise_for_status()
        return attachment_id

    def add_tags(self, transaction: FireflyTransaction, tags: list[str]) -> None:
        """Append tags to a transaction split (PUT replaces, so callers pass the full set)."""
        resp = self._client.put(
            f"/api/v1/transactions/{transaction.id}",
            json={
                "transactions": [
                    {"transaction_journal_id": transaction.journal_id, "tags": tags}
                ]
            },
        )
        resp.raise_for_status()

    # --- statement import (see banking.importer.StatementWriter) ---------------------------

    def list_accounts(self, account_type: str) -> list[FireflyAccount]:
        out: list[FireflyAccount] = []
        page = 1
        while True:
            resp = self._client.get(
                "/api/v1/accounts", params={"type": account_type, "page": page}
            )
            resp.raise_for_status()
            body = resp.json()
            for item in body["data"]:
                attrs = item["attributes"]
                out.append(
                    FireflyAccount(
                        id=str(item["id"]),
                        name=attrs["name"],
                        account_type=attrs.get("type", account_type),
                        iban=attrs.get("iban"),
                        currency_code=attrs.get("currency_code") or "EUR",
                        account_role=attrs.get("account_role"),
                    )
                )
            pagination = body.get("meta", {}).get("pagination", {})
            if page >= int(pagination.get("total_pages", page) or 1):
                break
            page += 1
        return out

    def ensure_asset_account(
        self,
        iban: str,
        currency: str,
        role: str,
        name: str,
        *,
        opening_balance: Decimal | None = None,
        opening_date: str | None = None,
    ) -> str:
        for account in self.list_accounts("asset"):
            if account.iban == iban:
                return account.id
        body: dict[str, object] = {
            "name": name,
            "type": "asset",
            "account_role": role,
            "iban": iban,
            "currency_code": currency,
        }
        if opening_balance is not None and opening_date:
            body["opening_balance"] = str(opening_balance)
            body["opening_balance_date"] = opening_date
        return self._create_account(body)

    def create_opposing_account(self, name: str, iban: str | None, role: str) -> str:
        body: dict[str, object] = {"name": name, "type": role}
        if iban:
            body["iban"] = iban
        return self._create_account(body)

    def _create_account(self, body: dict[str, object]) -> str:
        resp = self._client.post("/api/v1/accounts", json=body)
        resp.raise_for_status()
        return str(resp.json()["data"]["id"])

    def create_transaction(
        self, split: dict[str, object], *, skip_duplicates: bool
    ) -> str | None:
        """Create a transaction, returning the new transaction group id.

        Returns the created id (``data.id`` from the 200/201 response) on success, or ``None`` when
        Firefly rejected it as a duplicate. The id lets a later correction-capture pass key a label
        record back to the transaction it described.
        """
        resp = self._client.post(
            "/api/v1/transactions",
            json={
                "apply_rules": False,
                "fire_webhooks": False,
                "error_if_duplicate_hash": skip_duplicates,
                "transactions": [split],
            },
        )
        if resp.status_code == 422 and "uplicate" in resp.text:
            return None
        resp.raise_for_status()
        return str(resp.json()["data"]["id"])
