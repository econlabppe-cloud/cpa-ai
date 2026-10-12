"""WhatsApp Cloud API: קליטת קבלות וחשבוניות בווטסאפ, ותשובה ללקוח באותה שיחה.

הפורמט (webhook, חתימה, הורדת מדיה) מבוסס על תיעוד Meta כפי שאומת חלקית ממקורות משניים:
- אימות מנוי: GET עם hub.mode=subscribe, hub.verify_token, hub.challenge → מחזירים את ה-challenge.
- חתימה: HMAC-SHA256 של גוף הבקשה הגולמי עם סוד האפליקציה, בכותרת X-Hub-Signature-256 כ-"sha256=<hex>".
- הודעה: entry[].changes[].value.messages[] עם type = text | image | document | ...; למדיה יש id.
- הורדה: GET graph.facebook.com/{version}/{media_id} (עם Bearer) מחזיר url; GET ל-url עם Bearer מחזיר את הקובץ.
  הקישור פג אחרי כ-5 דקות ו-media id אחרי 7 ימים.
לא נבדק מול שירות חי. גרסת ה-Graph ניתנת להגדרה (CPA_AI_WHATSAPP_GRAPH_VERSION).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import select

from ..db import Business
from ..ingest import Attachment, IngestItem, IngestResult, claim_event, ingest

GRAPH_BASE = "https://graph.facebook.com"
HELP_TEXT = ("אפשר לשלוח לי כאן תמונה, PDF או טקסט של חשבונית או קבלה, ואני רושם אותה.\n"
             "פקודות: *סטטוס* — איפה אנחנו החודש · *מה חסר* — מה מחכה לך · *עזרה*")


def normalize_phone(raw: str) -> str:
    """ספרות בלבד בפורמט בינלאומי בלי '+': 050-1234567 → 972501234567."""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0"):
        digits = "972" + digits[1:]
    return digits


def verify_signature(raw_body: bytes, header: str | None, app_secret: str) -> bool:
    if not header or not header.startswith("sha256=") or not app_secret:
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


@dataclass
class IncomingMessage:
    id: str
    sender: str
    type: str
    text: str | None = None
    media_id: str | None = None
    mime: str | None = None
    filename: str | None = None


def parse_webhook(payload: dict) -> list[IncomingMessage]:
    out: list[IncomingMessage] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            for m in (change.get("value") or {}).get("messages", []) or []:
                mtype = m.get("type", "")
                media = m.get(mtype) if isinstance(m.get(mtype), dict) else {}
                out.append(IncomingMessage(
                    id=m.get("id", ""), sender=normalize_phone(m.get("from", "")), type=mtype,
                    text=(m.get("text") or {}).get("body") if mtype == "text" else media.get("caption"),
                    media_id=media.get("id") if mtype in ("image", "document") else None,
                    mime=media.get("mime_type"), filename=media.get("filename")))
    return [m for m in out if m.id]


class WhatsAppClient:
    """לקוח דק על ה-Graph API. `fetch` ניתן להחלפה (בדיקות)."""

    def __init__(self, token: str, version: str = "v21.0", fetch: Callable | None = None):
        self.token, self.version = token, version
        self._fetch = fetch or self._urllib_fetch

    @staticmethod
    def _urllib_fetch(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - כתובות Meta בלבד
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    @classmethod
    def from_env(cls) -> "WhatsAppClient | None":
        token = os.getenv("CPA_AI_WHATSAPP_TOKEN")
        return cls(token, os.getenv("CPA_AI_WHATSAPP_GRAPH_VERSION", "v21.0")) if token else None

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        status, body = self._fetch("GET", f"{GRAPH_BASE}/{self.version}/{media_id}", self._auth(), None)
        if status != 200:
            raise RuntimeError(f"שליפת מדיה נכשלה ({status})")
        meta = json.loads(body)
        url = meta.get("url", "")
        if not url.startswith("https://"):  # לא שולחים את הטוקן לכתובת שאינה HTTPS
            raise RuntimeError("כתובת מדיה לא תקינה")
        status, data = self._fetch("GET", url, self._auth(), None)
        if status != 200:
            raise RuntimeError(f"הורדת מדיה נכשלה ({status})")
        return data, meta.get("mime_type", "application/octet-stream")

    def send_text(self, phone_number_id: str, to: str, text: str) -> None:
        body = json.dumps({"messaging_product": "whatsapp", "to": to, "type": "text",
                           "text": {"body": text[:4000]}}).encode()
        self._fetch("POST", f"{GRAPH_BASE}/{self.version}/{phone_number_id}/messages",
                    {**self._auth(), "Content-Type": "application/json"}, body)


def _shekel(n) -> str:
    return f"₪{abs(n or 0):,.0f}"


def compose_reply(result: IngestResult) -> str | None:
    """תשובה קצרה וברורה בעברית. None = לא עונים (שליחה חוזרת של אותה הודעה)."""
    if result.skipped:
        return None
    lines: list[str] = []
    for d in result.documents:
        x, dec = d.extracted, d.decision
        who = x.get("vendor_name") or x.get("customer_name") or "מסמך"
        total = _shekel(dec.get("total_amount"))
        if d.status == "confirmed":
            lines.append(f"✅ נרשם: {who} · {total}")
        else:
            problems = [f["message"] for f in dec["findings"] if f["severity"] == "error"] or \
                       [f["message"] for f in dec["findings"] if f["severity"] == "warning"]
            lines.append(f"⚠️ נקלט, אבל צריך את עינייך: {who} · {total}")
            if problems:
                lines.append(f"   {problems[0]}")
            if any(f["code"] == "ALLOCATION_MISSING" for f in dec["findings"]):
                lines.append("   💡 כדאי לבקש מהספק חשבונית עם מספר הקצאה — בלי זה אי אפשר לנכות מע\"מ")
    for dup in result.duplicates:
        lines.append(f"ℹ️ את המסמך הזה כבר קיבלתי (#{dup['of']}) — לא ספרתי אותו פעמיים")
    if not lines:
        lines.append("לא זיהיתי חשבונית בהודעה. שלחו תמונה, PDF או טקסט של חשבונית או קבלה.")
    return "\n".join(lines)


def business_by_phone(s, phone: str) -> Business | None:
    return s.scalar(select(Business).where(Business.whatsapp_phone == phone))


def handle_message(s, msg: IncomingMessage, client: WhatsAppClient | None,
                   command_handler: Callable | None = None) -> str | None:
    """מטפל בהודעה אחת. מחזיר את טקסט התשובה (או None)."""
    biz = business_by_phone(s, msg.sender)
    if biz is None:
        return None  # מספר לא מחובר: לא עונים (מונע שימוש בנו כמשקף ספאם)

    if msg.type in ("image", "document") and msg.media_id:
        if not claim_event(s, "whatsapp", msg.id, biz.id):
            return None
        if client is None:
            return "החיבור ל-WhatsApp אינו מוגדר במלואו בשרת (חסר CPA_AI_WHATSAPP_TOKEN)"
        try:
            data, mime = client.download_media(msg.media_id)
        except Exception as e:  # מדיה פגה / שגיאת רשת
            s.commit()
            return f"לא הצלחתי להוריד את הקובץ ({e}). אפשר לשלוח אותו שוב?"
        name = msg.filename or ("image" if msg.type == "image" else "document")
        item = IngestItem(source="whatsapp", source_ref=msg.id, text=msg.text,
                          attachments=[Attachment(name, msg.mime or mime, data)], sender=msg.sender)
        res = ingest(s, biz, item, f"whatsapp:{msg.sender}", already_claimed=True)
        return compose_reply(res)

    if msg.type == "text" and msg.text:
        word = msg.text.strip()
        if command_handler is not None:
            if not claim_event(s, "whatsapp", msg.id, biz.id):
                return None
            reply = command_handler(s, biz, word)
            if reply is not None:
                s.commit()
                return reply
            item = IngestItem(source="whatsapp", source_ref=msg.id, text=word, sender=msg.sender)
            return compose_reply(ingest(s, biz, item, f"whatsapp:{msg.sender}", already_claimed=True))
        item = IngestItem(source="whatsapp", source_ref=msg.id, text=word, sender=msg.sender)
        return compose_reply(ingest(s, biz, item, f"whatsapp:{msg.sender}"))

    if not claim_event(s, "whatsapp", msg.id, biz.id):
        return None
    s.commit()
    return "אני קורא חשבוניות וקבלות כתמונה, PDF או טקסט. " + HELP_TEXT
