"""The read-surface abstraction for incoming documents.

`AttachmentSource` is the seam between the orchestration and wherever attachments come from
(an IMAP inbox today; a folder, an API, or a fake in tests).
"""

from __future__ import annotations

from typing import Protocol

from firefly_bot.config import ImapSettings
from firefly_bot.ingest.imap import fetch_attachments
from firefly_bot.models import Attachment


class AttachmentSource(Protocol):
    def fetch(self) -> list[Attachment]: ...


class ImapAttachmentSource:
    """Default source: pull document attachments from an IMAP mailbox."""

    def __init__(self, settings: ImapSettings) -> None:
        self._settings = settings

    def fetch(self) -> list[Attachment]:
        return fetch_attachments(self._settings)
