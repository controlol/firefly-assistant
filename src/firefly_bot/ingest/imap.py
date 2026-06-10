"""Low-level IMAP helpers (stdlib only).

Messages are addressed by UID (stable across the session) so a message can be fetched and later
marked processed. "Processed" is a custom IMAP keyword — the email is flagged, never deleted.
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


def connect(settings: ImapSettings) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(settings.host, settings.port)
    conn.login(settings.username, settings.password.get_secret_value())
    conn.select(settings.mailbox)
    return conn


def search_unprocessed(conn: imaplib.IMAP4_SSL, keyword: str) -> list[bytes]:
    """UIDs of messages not yet marked with the processed keyword."""
    typ, data = conn.uid("search", "UNKEYWORD", keyword)
    if typ != "OK" or not data or not data[0]:
        return []
    uids: list[bytes] = data[0].split()
    return uids


def fetch_attachments_for(
    conn: imaplib.IMAP4_SSL, uid: bytes, settings: ImapSettings
) -> list[Attachment]:
    uid_str = uid.decode()
    typ, msg_data = conn.uid("fetch", uid_str, "(RFC822)")
    if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
        return []
    message = email.message_from_bytes(msg_data[0][1])
    return _extract_parts(message, uid_str, settings)


def mark_processed(conn: imaplib.IMAP4_SSL, uid: str, keyword: str) -> None:
    conn.uid("store", uid, "+FLAGS", f"({keyword})")


def _extract_parts(message: Message, uid: str, settings: ImapSettings) -> list[Attachment]:
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
                source_uid=uid,
            )
        )
    return out


def _parse_date(raw: str | None) -> datetime:
    if raw:
        parsed = email.utils.parsedate_to_datetime(raw)
        if parsed is not None:
            return parsed
    return datetime.now(UTC)
