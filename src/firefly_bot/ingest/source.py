"""The read-surface abstraction for incoming documents.

`AttachmentSource` is the seam between the orchestration and wherever attachments come from
(an IMAP inbox today; a folder, an API, or a fake in tests). `mark_processed` is called only
after a document has been attached, so unmatched emails stay unprocessed and get retried.
"""

from __future__ import annotations

import hashlib
import imaplib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from firefly_bot.config import ImapSettings
from firefly_bot.ingest import imap
from firefly_bot.models import Attachment

log = logging.getLogger("firefly_bot.imap")

_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


class AttachmentSource(Protocol):
    def fetch(self) -> list[Attachment]: ...
    def mark_processed(self, attachment: Attachment) -> None: ...
    def close(self) -> None: ...


class ImapAttachmentSource:
    """Pull unprocessed document attachments from an IMAP mailbox; flag them once attached."""

    def __init__(self, settings: ImapSettings) -> None:
        self._settings = settings
        self._conn: imaplib.IMAP4_SSL | None = None
        self._moved: set[str] = set()
        # Messages that arrived without a usable attachment — a human mistake to surface.
        self.skipped: list[tuple[str, str]] = []

    def fetch(self) -> list[Attachment]:
        self._conn = imap.connect(self._settings)
        imap.ensure_folder(self._conn, self._settings.processed_folder)
        self.skipped = []
        self._moved = set()
        out: list[Attachment] = []
        for uid in imap.search_messages(self._conn):
            message = imap.fetch_message(self._conn, uid)
            if message is None:
                continue
            attachments = imap.extract_attachments(message, uid.decode(), self._settings)
            if attachments:
                out.extend(attachments)
            else:
                subject = str(message.get("Subject", "(no subject)"))
                self.skipped.append((uid.decode(), subject))
                log.warning(
                    "Email uid %s %r has no usable attachment — left in inbox for review",
                    uid.decode(),
                    subject,
                )
        return out

    def mark_processed(self, attachment: Attachment) -> None:
        """Move the email to the processed folder (once per message)."""
        uid = attachment.source_uid
        if self._conn is None or uid is None or uid in self._moved:
            return
        imap.move(self._conn, uid, self._settings.processed_folder)
        self._moved.add(uid)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except OSError:
                pass
            self._conn = None


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

    def mark_processed(self, attachment: Attachment) -> None:
        return None

    def close(self) -> None:
        return None
