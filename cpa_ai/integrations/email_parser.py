"""פענוח הודעת מייל (RFC 822) ל-IngestItem. בלי תלות ברשת."""
from __future__ import annotations

import base64
import email
import hashlib
import re
from email import policy
from email.message import EmailMessage

from ..ingest import Attachment, IngestItem

_PLUS_TOKEN = re.compile(r"\+([A-Za-z0-9_\-]{8,64})@")


def recipient_token(msg: EmailMessage) -> str | None:
    """טוקן העסק מכתובת עם plus-addressing (inbox+TOKEN@gmail.com) או מ-local-part ייעודי."""
    for header in ("Delivered-To", "X-Original-To", "To", "Cc"):
        for value in msg.get_all(header, []):
            m = _PLUS_TOKEN.search(str(value))
            if m:
                return m.group(1)
    return None


def parse_email(raw: bytes, source: str = "email") -> tuple[IngestItem, EmailMessage]:
    msg: EmailMessage = email.message_from_bytes(raw, policy=policy.default)  # type: ignore[assignment]
    message_id = (msg.get("Message-ID") or "").strip()
    ref = message_id or "sha256:" + hashlib.sha256(raw).hexdigest()

    body_part = msg.get_body(preferencelist=("plain", "html"))
    text = body_part.get_content() if body_part is not None else None

    attachments: list[Attachment] = []
    for part in msg.iter_attachments():
        data = part.get_payload(decode=True)
        if data:
            attachments.append(Attachment(part.get_filename() or "attachment", part.get_content_type(), data))

    return IngestItem(source=source, source_ref=ref[:200], text=text, attachments=attachments,
                      sender=str(msg.get("From") or ""), subject=str(msg.get("Subject") or "")), msg


def item_from_json(payload: dict) -> IngestItem:
    """תבנית מנורמלת לספקי דואר נכנס (Postmark/SendGrid-like):
    {message_id, from, subject, text, html, attachments:[{filename, content_type, content_base64}]}"""
    atts = []
    for a in payload.get("attachments", []) or []:
        atts.append(Attachment(a.get("filename") or "attachment", a.get("content_type") or "application/octet-stream",
                               base64.b64decode(a.get("content_base64") or "")))
    body = payload.get("text") or payload.get("html") or None
    raw = repr((payload.get("from"), payload.get("subject"), body, [a.filename for a in atts])).encode()
    ref = payload.get("message_id") or "sha256:" + hashlib.sha256(raw).hexdigest()
    return IngestItem(source="email", source_ref=str(ref)[:200], text=body, attachments=atts,
                      sender=payload.get("from"), subject=payload.get("subject"))
