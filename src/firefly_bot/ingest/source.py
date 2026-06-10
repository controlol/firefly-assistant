"""The read-surface abstraction for incoming documents.

`AttachmentSource` is the seam between the orchestration and wherever attachments come from
(an IMAP inbox today; a folder, an API, or a fake in tests).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from firefly_bot.config import ImapSettings
from firefly_bot.ingest.imap import fetch_attachments
from firefly_bot.models import Attachment

_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


class AttachmentSource(Protocol):
    def fetch(self) -> list[Attachment]: ...


class ImapAttachmentSource:
    """Default source: pull document attachments from an IMAP mailbox."""

    def __init__(self, settings: ImapSettings) -> None:
        self._settings = settings

    def fetch(self) -> list[Attachment]:
        return fetch_attachments(self._settings)


class FolderAttachmentSource:
    """Local-folder source for testing — reads documents from a directory instead of email."""

    def __init__(self, folder: str) -> None:
        self._folder = Path(folder)

    def fetch(self) -> list[Attachment]:
        out: list[Attachment] = []
        for path in sorted(self._folder.iterdir()):
            content_type = _CONTENT_TYPES.get(path.suffix.lower())
            if not path.is_file() or content_type is None:
                continue
            data = path.read_bytes()
            out.append(
                Attachment(
                    filename=path.name,
                    content_type=content_type,
                    data=data,
                    sha256=hashlib.sha256(data).hexdigest(),
                    source_message_id=f"file://{path.name}",
                    received_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
                )
            )
        return out
