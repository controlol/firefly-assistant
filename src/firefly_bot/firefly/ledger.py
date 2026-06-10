"""The write-surface abstraction the pipeline depends on.

`Ledger` is the seam between the orchestration and Firefly III. `FireflyClient` satisfies it
structurally (no inheritance needed); `DryRunLedger` wraps a real ledger to read live data but
suppress every write — which is exactly what `--dry-run` needs, and what lets the pipeline be
tested with fakes.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Protocol

from firefly_bot.models import Attachment, FireflyTransaction

log = logging.getLogger("firefly_bot.ledger")


class Ledger(Protocol):
    """Everything the pipeline needs from Firefly III."""

    def list_transactions(self, *, start: date, end: date) -> list[FireflyTransaction]: ...

    def attach_document(self, transaction: FireflyTransaction, attachment: Attachment) -> str: ...

    def add_tags(self, transaction: FireflyTransaction, tags: list[str]) -> None: ...

    def close(self) -> None: ...


class DryRunLedger:
    """Read-through, write-suppressing decorator over a real `Ledger`.

    Reads (`list_transactions`) are delegated so matching runs against live data; writes are
    recorded on `attached` / `tagged` and logged, but never sent. Use for `--dry-run` and tests.
    """

    def __init__(self, inner: Ledger) -> None:
        self._inner = inner
        self.attached: list[tuple[str, str]] = []  # (journal_id, filename)
        self.tagged: list[tuple[str, list[str]]] = []  # (transaction_id, tags)

    def list_transactions(self, *, start: date, end: date) -> list[FireflyTransaction]:
        return self._inner.list_transactions(start=start, end=end)

    def attach_document(self, transaction: FireflyTransaction, attachment: Attachment) -> str:
        self.attached.append((transaction.journal_id, attachment.filename))
        log.info(
            "[dry-run] would attach %s to journal %s",
            attachment.filename,
            transaction.journal_id,
        )
        return "dry-run-attachment-id"

    def add_tags(self, transaction: FireflyTransaction, tags: list[str]) -> None:
        self.tagged.append((transaction.id, list(tags)))
        log.info("[dry-run] would tag txn %s with %s", transaction.id, tags)

    def close(self) -> None:
        self._inner.close()
