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

# Allow `UID MOVE` (RFC 6851) through imaplib; Dovecot and most servers support it.
imaplib.Commands.setdefault("MOVE", ("SELECTED",))


def connect(settings: ImapSettings) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(settings.host, settings.port)
    conn.login(settings.username, settings.password.get_secret_value())
    conn.select(settings.mailbox)
    return conn


def search_messages(conn: imaplib.IMAP4_SSL) -> list[bytes]:
    """UIDs of all messages in the selected mailbox (processed mail is moved out)."""
    typ, data = conn.uid("search", "ALL")
    if typ != "OK" or not data or not data[0]:
        return []
    uids: list[bytes] = data[0].split()
    return uids


def ensure_folder(conn: imaplib.IMAP4_SSL, folder: str) -> None:
    conn.create(folder)  # NO if it already exists — harmless
    conn.subscribe(folder)  # so Roundcube shows it


def move(conn: imaplib.IMAP4_SSL, uid: str, folder: str) -> None:
    """Move a message to another folder (kept, not deleted). Falls back to copy+delete."""
    try:
        typ, _ = conn.uid("MOVE", uid, folder)
        if typ == "OK":
            return
    except imaplib.IMAP4.error:
        pass
    conn.uid("COPY", uid, folder)
    conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
    conn.expunge()


def fetch_message(conn: imaplib.IMAP4_SSL, uid: bytes) -> Message | None:
    typ, msg_data = conn.uid("fetch", uid.decode(), "(RFC822)")
    if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
        return None
    return email.message_from_bytes(msg_data[0][1])


def extract_attachments(message: Message, uid: str, settings: ImapSettings) -> list[Attachment]:
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
