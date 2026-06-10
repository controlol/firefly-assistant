"""Minimal typed Firefly III API client (only the endpoints firefly-bot needs).

Docs: https://api-docs.firefly-iii.org/ — uses a Personal Access Token (Bearer).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from firefly_bot.config import FireflySettings
from firefly_bot.models import Attachment, FireflyTransaction


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
                    category_name=split.get("category_name"),
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
