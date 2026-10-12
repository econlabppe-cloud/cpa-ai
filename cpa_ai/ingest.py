"""צינור קליטה אחד לכל הערוצים (מייל, WhatsApp, העלאה).

שלבים: אידמפוטנטיות → פענוח קבצים → חילוץ → דה-דופליקציה → כללי מס → שמירה (כולל קובץ מקור).
כל ערוץ רק מתרגם את ההודעה שלו ל-IngestItem; הכללים זהים.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import extraction, services, storage
from .db import Business, Document, InboundEvent
from .extraction import openai_llm
from .integrations.pdf_text import extract_pdf_text
from .schemas import ExtractedDocument


@dataclass
class Attachment:
    filename: str
    mime: str
    data: bytes


@dataclass
class IngestItem:
    source: str                 # email | whatsapp | upload | api
    source_ref: str             # מזהה ייחודי של ההודעה בערוץ (Message-ID / wamid)
    text: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    sender: str | None = None
    subject: str | None = None


@dataclass
class IngestResult:
    skipped: bool = False
    documents: list[Document] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    return "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.splitlines() if ln.strip())


def sniff(data: bytes) -> tuple[str | None, str]:
    """סוג הקובץ לפי התוכן, לא לפי מה שהשולח טוען (שם קובץ או Content-Type ניתנים לזיוף).
    מחזיר (סוג, mime בטוח). svg ו-html נחשבים טקסט, כך שלעולם לא יוגשו כדפים או תמונות פעילים."""
    if data[:5] == b"%PDF-":
        return "pdf", "application/pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "image", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image", "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image", "image/webp"
    head = data[:4000]
    if b"\x00" not in head:
        try:
            head.decode("utf-8")
            return "text", "text/plain"
        except UnicodeDecodeError:
            try:
                data[:4000].decode("cp1255")
                return "text", "text/plain"
            except UnicodeDecodeError:
                pass
    return None, "application/octet-stream"


MIN_EMAIL_IMAGE_BYTES = 10_000  # לוגואים ופיקסלי מעקב בחתימות מייל קטנים מזה


def _looks_like_invoice(e: ExtractedDocument, strict: bool = False) -> bool:
    """strict (מיילים): נדרש גם סוג מסמך מפורש וגם סכום — כדי שניוזלטר עם "סה"כ לתשלום" לא ייקלט.
    בווטסאפ/העלאה המשתמש שלח במכוון, ולכן מקלים."""
    if strict:
        return e.doc_type != "unknown" and e.total_amount is not None
    return e.doc_type != "unknown" or e.total_amount is not None


def _existing(s: Session, business_id: int, **where) -> Document | None:
    q = select(Document).where(Document.business_id == business_id, Document.status != "rejected")
    for k, v in where.items():
        q = q.where(getattr(Document, k) == v)
    return s.scalars(q.limit(1)).first()


def _dedupe_key(e: ExtractedDocument) -> str | None:
    if e.vendor_id and e.doc_number and e.total_amount is not None:
        return f"{e.vendor_id.zfill(9)}|{e.doc_number}|{e.total_amount:.2f}"
    return None


def _possible_duplicate(s: Session, business_id: int, e: ExtractedDocument) -> Document | None:
    if not (e.vendor_id and e.doc_date and e.total_amount is not None):
        return None
    for d in s.scalars(select(Document).where(
            Document.business_id == business_id, Document.status != "rejected", Document.doc_date == e.doc_date)):
        x = d.extracted
        if x.get("vendor_id") == e.vendor_id and x.get("total_amount") == e.total_amount:
            return d
    return None


def _store_one(s: Session, biz: Business, item: IngestItem, extracted: ExtractedDocument, extractor: str,
               raw_text: str | None, att: Attachment | None, content_hash: str, result: IngestResult,
               actor: str, force_review: str | None = None) -> None:
    dup = _existing(s, biz.id, content_hash=content_hash)
    reason = "קובץ זהה כבר נקלט"
    key = _dedupe_key(extracted)
    if dup is None and key:
        dup = _existing(s, biz.id, dedupe_key=key)
        reason = "אותו ספק, מספר מסמך וסכום כבר נקלטו"
    if dup is not None:
        result.duplicates.append({"of": dup.id, "reason": reason, "source": item.source})
        services.audit(s, actor, "ingest_duplicate", "document", dup.id, source=item.source, reason=reason)
        return

    meta = {"source": item.source, "source_ref": item.source_ref, "content_hash": content_hash, "dedupe_key": key}
    if att is not None:
        rel, _ = storage.save(att.data, att.filename)
        meta.update(attachment_path=rel, attachment_name=att.filename[:200], attachment_mime=att.mime[:100])
    maybe = _possible_duplicate(s, biz.id, extracted)
    if maybe is not None and not force_review:
        force_review = f"ייתכן שזו כפילות של מסמך #{maybe.id} (אותו ספק, תאריך וסכום) — לבדיקה"
    doc = services._store_expense(s, biz, extracted, extractor, raw_text, actor, meta=meta, force_review=force_review)
    result.documents.append(doc)


def claim_event(s: Session, source: str, source_ref: str, business_id: int | None) -> bool:
    """True אם זו הפעם הראשונה שמטפלים בהודעה. שליחה חוזרת של webhook מחזירה False."""
    try:
        with s.begin_nested():
            s.add(InboundEvent(source=source, source_ref=source_ref, business_id=business_id))
            s.flush()
        return True
    except IntegrityError:
        return False


def ingest(s: Session, biz: Business, item: IngestItem, actor: str, mode: str = "auto",
           already_claimed: bool = False) -> IngestResult:
    result = IngestResult()
    if not already_claimed and not claim_event(s, item.source, item.source_ref, biz.id):
        result.skipped = True
        result.notes.append("הודעה זו כבר טופלה (שליחה חוזרת)")
        return result

    handled = False
    strict = item.source == "email"
    # תמונות אחרונות: במייל הן לרוב לוגו, ונדלג עליהן אם כבר נקלט מסמך אחר באותו מייל
    prepared = []
    for att in item.attachments:
        kind, safe_mime = sniff(att.data)
        prepared.append((kind, Attachment(att.filename, safe_mime, att.data)))
    for kind, att in sorted(prepared, key=lambda p: p[0] == "image"):
        digest = storage.sha256(att.data)
        if kind == "pdf":
            text, info = extract_pdf_text(att.data)
            if not text:
                _store_one(s, biz, item, ExtractedDocument(doc_type="unknown", extraction_confidence=0.0),
                           "pdf-unreadable", None, att, digest, result, actor,
                           force_review=f"{info['reason']} — יש להזין ידנית או להפעיל חילוץ בתמונה")
            else:
                e, extractor = extraction.extract_text(text, mode, biz.llm_consent)
                force = None if info["reliable"] else f"{info['reason']} — לבדיקה"
                _store_one(s, biz, item, e, f"pdf+{extractor}", text, att, digest, result, actor, force_review=force)
            handled = True
        elif kind == "text":
            raw = att.data.decode("utf-8", errors="replace")
            text = html_to_text(raw) if re.search(r"(?i)<\s*(html|body|div|p|table|br)\b", raw) else raw
            e, extractor = extraction.extract_text(text, mode, biz.llm_consent)
            if _looks_like_invoice(e, strict):
                _store_one(s, biz, item, e, extractor, text, att, digest, result, actor)
                handled = True
            else:
                result.notes.append(f"לא זוהתה חשבונית בקובץ: {att.filename}")
        elif kind == "image":
            if strict and (handled or len(att.data) < MIN_EMAIL_IMAGE_BYTES):
                result.notes.append(f"תמונה במייל דולגה (כנראה לוגו): {att.filename}")
                continue
            if biz.llm_consent and openai_llm.available():
                e = openai_llm.extract(image=att.data, mime=att.mime)
                _store_one(s, biz, item, e, "openai-vision", None, att, digest, result, actor)
            else:
                _store_one(s, biz, item, ExtractedDocument(doc_type="unknown", extraction_confidence=0.0),
                           "image-manual", None, att, digest, result, actor,
                           force_review="תמונה נשמרה אך לא חולצה (אין חילוץ בתמונה) — יש להזין את הנתונים ידנית")
            handled = True
        else:
            result.notes.append(f"קובץ מסוג לא נתמך דולג: {att.filename}")

    if not handled and item.text:
        body = html_to_text(item.text) if "<" in item.text and ">" in item.text else item.text
        e, extractor = extraction.extract_text(body, mode, biz.llm_consent)
        if _looks_like_invoice(e, strict):
            _store_one(s, biz, item, e, extractor, body, None, storage.sha256(body.encode()), result, actor)
            handled = True
    if not handled:
        result.notes.append("לא זוהתה חשבונית בהודעה")

    event = s.scalar(select(InboundEvent).where(InboundEvent.source == item.source,
                                                InboundEvent.source_ref == item.source_ref))
    if event:
        event.outcome = f"docs={len(result.documents)} dup={len(result.duplicates)}"
    s.commit()
    return result
