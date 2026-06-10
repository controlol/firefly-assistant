"""IMAP ingestion: pull candidate document attachments from a mailbox.

Uses only the stdlib (`imaplib`, `email`) to keep dependencies light. Returns typed
`Attachment` objects; dedup is by SHA-256 so re-running over an already-seen message is safe.
"""

from __future__ import annotations

import email
import email.utils
import hashlib
import imaplib
from datetime import UTC, datetime
from email.message import Message

from firefly_bot.config import ImapSettings
from firefly_bot.models import Attachment


def fetch_attachments(settings: ImapSettings) -> list[Attachment]:
    """Connect, fetch unseen messages, and return their document attachments."""
    attachments: list[Attachment] = []
    with _connect(settings) as conn:
        conn.select(settings.mailbox)
        typ, data = conn.search(None, "UNSEEN")
        if typ != "OK":
            return attachments
        for num in data[0].split():
            typ, msg_data = conn.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            message = email.message_from_bytes(msg_data[0][1])
            attachments.extend(_extract_parts(message, settings))
            if settings.mark_seen:
                conn.store(num, "+FLAGS", "\\Seen")
    return attachments


def _connect(settings: ImapSettings) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(settings.host, settings.port)
    conn.login(settings.username, settings.password.get_secret_value())
    return conn


def _extract_parts(message: Message, settings: ImapSettings) -> list[Attachment]:
    out: list[Attachment] = []
    message_id = message.get("Message-ID", "")
    received_at = _parse_date(message.get("Date"))
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get("Content-Disposition") is None:
            continue
        content_type = part.get_content_type()
        if content_type not in settings.allowed_content_types:
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        out.append(
            Attachment(
                filename=part.get_filename() or "attachment",
                content_type=content_type,
                data=payload,
                sha256=hashlib.sha256(payload).hexdigest(),
                source_message_id=message_id,
                received_at=received_at,
            )
        )
    return out


def _parse_date(raw: str | None) -> datetime:
    if raw:
        parsed = email.utils.parsedate_to_datetime(raw)
        if parsed is not None:
            return parsed
    return datetime.now(UTC)
