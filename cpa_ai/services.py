"""שכבת השירות: קליטה → מנוע כללים → בקרת איכות → הכנת דוח → אישור מייצג.

עיקרון: המערכת לעולם אינה משדרת לרשויות בעצמה. ייצוג מול רשות המסים מותר רק
לרואה חשבון, יועץ מס או עורך דין (חוק הסדרת העיסוק בייעוץ מס, התשס"ה-2005),
ולכן כל דוח עובר אישור של מייצג מורשה, שנחתם על hash של תוכן הדוח.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import extraction
from .db import AuditLog, Business, Document, Report, VendorRule, utcnow
from .reports import periodic, unified_file
from .rules import engine
from .rules.checks import validate_israeli_id
from .schemas import ApprovalIn, BusinessIn, ExtractedDocument, IncomeIn, ReviewIn


# ממצאים שהשלכתם כבר מגולמת בחישוב (המע"מ לא מנוכה) — הבודק רשאי לאשר את ההוצאה למרות קיומם
NON_BLOCKING_ON_REVIEW = {"ALLOCATION_MISSING", "ALLOCATION_MALFORMED"}


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    pass


GENESIS = "GENESIS"


def _canonical_ts(ts: datetime) -> str:
    # SQLite מחזיר datetime ללא אזור זמן; מניחים UTC כך שהחישוב יחזור זהה אחרי קריאה מהמסד
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts.isoformat()


def _row_hash(row: AuditLog) -> str:
    payload = {
        "id": row.id, "ts": _canonical_ts(row.ts), "actor": row.actor, "action": row.action,
        "entity": row.entity, "entity_id": row.entity_id, "details": row.details, "prev": row.prev_hash,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def audit(s: Session, actor: str, action: str, entity: str, entity_id: int | None, **details) -> None:
    """רשומה מקושרת לקודמתה בשרשרת hash: שינוי או מחיקה של שורה שוברים את השרשרת (ראו verify_audit)."""
    prev = s.scalar(select(AuditLog.row_hash).order_by(AuditLog.id.desc()).limit(1)) or GENESIS
    row = AuditLog(actor=actor, action=action, entity=entity, entity_id=entity_id, details=details,
                   ts=datetime.now(timezone.utc), prev_hash=prev, row_hash="")
    s.add(row)
    s.flush()
    row.row_hash = _row_hash(row)


def verify_audit(s: Session) -> dict:
    prev = GENESIS
    count = 0
    for row in s.scalars(select(AuditLog).order_by(AuditLog.id)):
        count += 1
        if row.prev_hash != prev or _row_hash(row) != row.row_hash:
            return {"ok": False, "rows": count, "broken_at": row.id}
        prev = row.row_hash
    return {"ok": True, "rows": count, "broken_at": None}


def get(s: Session, model, id_: int):
    obj = s.get(model, id_)
    if obj is None:
        raise NotFound(f"{model.__tablename__}/{id_} לא נמצא")
    return obj


# --- עסקים -----------------------------------------------------------------
def create_business(s: Session, data: BusinessIn, actor: str) -> Business:
    if not validate_israeli_id(data.vat_id):
        raise ValueError(f"מספר עוסק {data.vat_id} אינו תקין (ספרת ביקורת)")
    biz = Business(**data.model_dump())
    biz.vat_id = biz.vat_id.zfill(9)
    s.add(biz)
    s.flush()
    audit(s, actor, "create", "business", biz.id, llm_consent=data.llm_consent)
    s.commit()
    return biz


def update_consent(s: Session, business_id: int, consent: bool, actor: str) -> Business:
    biz = get(s, Business, business_id)
    biz.llm_consent = consent
    audit(s, actor, "consent_change", "business", biz.id, llm_consent=consent)
    s.commit()
    return biz


# --- מסמכים ----------------------------------------------------------------
def vendor_category(s: Session, business_id: int, vendor_id: str | None) -> str | None:
    if not vendor_id:
        return None
    rule = s.scalar(select(VendorRule).where(VendorRule.business_id == business_id,
                                              VendorRule.vendor_id == vendor_id.zfill(9)))
    return rule.category if rule else None


def _store_expense(s: Session, biz: Business, extracted: ExtractedDocument, extractor: str,
                   raw_text: str | None, actor: str, *, meta: dict | None = None,
                   force_review: str | None = None) -> Document:
    """meta: source/source_ref/content_hash/dedupe_key/attachment_* (ראו ingest.py).
    force_review: טקסט ממצא שמכריח בדיקה אנושית (למשל תמונה שלא חולצה)."""
    _assert_period_open(s, biz.id, extracted.doc_date)
    remembered = vendor_category(s, biz.id, extracted.vendor_id)
    decision = engine.evaluate_expense(extracted, remembered)
    findings = list(decision.findings)
    if remembered:
        findings.append({"code": "VENDOR_MEMORY", "severity": "info",
                         "message": f"הסיווג נלקח מזיכרון הספק (קבעת בעבר: {remembered})", "source": ""})
    needs_review = decision.needs_human_review
    if force_review:
        findings.append({"code": "FORCED_REVIEW", "severity": "warning", "message": force_review, "source": ""})
        needs_review = True
    decision = decision.model_copy(update={"findings": findings, "needs_human_review": needs_review})
    doc = Document(
        business_id=biz.id, direction="expense",
        status="pending_review" if needs_review else "confirmed",
        extractor=extractor, doc_date=extracted.doc_date,
        extracted=extracted.model_dump(mode="json"), decision=decision.model_dump(mode="json"),
        raw_text=raw_text, **(meta or {}),
    )
    s.add(doc)
    s.flush()
    audit(s, actor, "ingest", "document", doc.id, extractor=extractor, status=doc.status,
          confidence=decision.confidence)
    s.commit()
    return doc


def process_text(s: Session, business_id: int, text: str, mode: str, actor: str) -> Document:
    biz = get(s, Business, business_id)
    extracted, extractor = extraction.extract_text(text, mode, biz.llm_consent)
    return _store_expense(s, biz, extracted, extractor, text, actor)


def process_image(s: Session, business_id: int, image: bytes, mime: str, actor: str) -> Document:
    biz = get(s, Business, business_id)
    if not biz.llm_consent:
        raise PermissionError("פענוח תמונה מחייב ספק LLM חיצוני — העסק לא נתן הסכמה")
    extracted = extraction.openai_llm.extract(image=image, mime=mime)
    return _store_expense(s, biz, extracted, "openai-vision", None, actor)


def add_income(s: Session, business_id: int, inc: IncomeIn, actor: str) -> Document:
    biz = get(s, Business, business_id)
    _assert_period_open(s, biz.id, inc.doc_date)
    decision = engine.evaluate_income(inc.doc_date, inc.doc_type, inc.pre_vat_amount, inc.vat_amount,
                                      inc.customer_id, inc.allocation_number)
    doc = Document(
        business_id=biz.id, direction="income",
        status="pending_review" if decision["needs_human_review"] else "confirmed",
        extractor="manual", doc_date=inc.doc_date, extracted=inc.model_dump(mode="json"), decision=decision,
    )
    s.add(doc)
    s.flush()
    audit(s, actor, "ingest", "document", doc.id, extractor="manual", direction="income", status=doc.status)
    s.commit()
    return doc


def list_documents(s: Session, business_id: int, status: str | None, limit: int = 100,
                   offset: int = 0) -> list[Document]:
    q = select(Document).where(Document.business_id == business_id)
    if status:
        q = q.where(Document.status == status)
    return list(s.scalars(q.order_by(Document.id.desc()).limit(limit).offset(offset)))


def _assert_period_open(s: Session, business_id: int, doc_date: date | None) -> None:
    """אחרי אישור מייצג התקופה נעולה: הוספה או שינוי של מסמך בתוכה היה משנים דוח שכבר נחתם."""
    if doc_date is None:
        return
    locked = s.scalar(select(Report).where(
        Report.business_id == business_id, Report.status == "approved",
        Report.period_start <= doc_date, Report.period_end >= doc_date))
    if locked is not None:
        raise Conflict(f"התאריך בתקופה של דוח מאושר (#{locked.id}) — לא ניתן להוסיף או לשנות. יש לרשום בתקופה הבאה")


def _remember_vendor(s: Session, business_id: int, extracted: ExtractedDocument, category: str, who: str) -> None:
    vid = extracted.vendor_id.zfill(9)
    rule = s.scalar(select(VendorRule).where(VendorRule.business_id == business_id, VendorRule.vendor_id == vid))
    if rule:
        rule.category, rule.created_by = category, who
    else:
        s.add(VendorRule(business_id=business_id, vendor_id=vid, vendor_name=extracted.vendor_name,
                         category=category, created_by=who))


def review_document(s: Session, doc_id: int, rv: ReviewIn, actor: str) -> Document:
    doc = get(s, Document, doc_id)
    _assert_period_open(s, doc.business_id, doc.doc_date)
    if doc.status == "rejected" and rv.action == "confirm":
        raise Conflict("מסמך שנדחה אינו ניתן לאישור; יש להזין אותו מחדש")
    if rv.action == "reject":
        doc.status = "rejected"
    else:
        if doc.direction == "expense":
            extracted = rv.corrections or ExtractedDocument.model_validate(doc.extracted)
            decision = engine.evaluate_expense(extracted, rv.category_override)
            blocking = [f for f in decision.findings
                        if f["severity"] == "error" and f["code"] not in NON_BLOCKING_ON_REVIEW]
            if blocking:
                raise Conflict("לא ניתן לאשר מסמך עם שגיאות חוסמות: " + "; ".join(f["message"] for f in blocking))
            doc.extracted = extracted.model_dump(mode="json")
            doc.decision = decision.model_dump(mode="json")
            doc.doc_date = extracted.doc_date
            if rv.category_override and extracted.vendor_id and decision.vendor_id_valid:
                _remember_vendor(s, doc.business_id, extracted, rv.category_override, rv.reviewer)
        elif any(f["severity"] == "error" for f in doc.decision["findings"]):
            raise Conflict("מסמך הכנסה עם שגיאות — יש להזין אותו מחדש עם נתונים מתוקנים")
        doc.status = "confirmed"
    doc.reviewed_by = rv.reviewer
    audit(s, actor, f"review_{rv.action}", "document", doc.id, reviewer=rv.reviewer,
          category_override=rv.category_override, corrected=rv.corrections is not None, note=rv.note)
    s.commit()
    return doc


# --- דוחות -----------------------------------------------------------------
def _build(s: Session, biz: Business, kind: str, start: date, end: date, credit_points: float) -> dict:
    if kind == "vat_period":
        return periodic.build_vat_period(s, biz, start, end)
    if kind == "income_tax_advance":
        return periodic.build_income_tax_advance(s, biz, start, end)
    if kind == "annual_estimate":
        return periodic.build_annual_estimate(s, biz, start.year, credit_points)
    raise ValueError(f"סוג דוח לא מוכר: {kind}")


def prepare_report(s: Session, business_id: int, kind: str, year: int, month: int | None,
                   credit_points: float, actor: str) -> Report:
    biz = get(s, Business, business_id)
    if kind == "annual_estimate":
        start, end = date(year, 1, 1), date(year, 12, 31)
    else:
        if month is None:
            raise ValueError("נדרש חודש לדוח תקופתי")
        start, end = periodic.vat_period_bounds(year, month, biz.vat_frequency)
    payload = _build(s, biz, kind, start, end, credit_points)
    payload["credit_points_input"] = credit_points

    for old in s.scalars(select(Report).where(
            Report.business_id == biz.id, Report.kind == kind, Report.period_start == start,
            Report.status == "prepared")):
        old.status = "superseded"
    rep = Report(business_id=biz.id, kind=kind, period_start=start, period_end=end,
                 payload=payload, content_hash=periodic.content_hash(payload), status="prepared")
    s.add(rep)
    s.flush()
    audit(s, actor, "prepare", "report", rep.id, kind=kind, hash=rep.content_hash)
    s.commit()
    return rep


def approve_report(s: Session, report_id: int, ap: ApprovalIn, actor: str) -> Report:
    rep = get(s, Report, report_id)
    if rep.status != "prepared":
        raise Conflict(f"דוח בסטטוס '{rep.status}' אינו ניתן לאישור")
    if not ap.attest:
        raise Conflict("נדרש אישור מפורש שהדוח נבדק ושהמאשר נושא באחריות המקצועית")
    if rep.payload.get("pending_document_ids"):
        raise Conflict("יש מסמכים בתקופה שממתינים לבדיקה — יש לטפל בהם ולהכין את הדוח מחדש")
    biz = get(s, Business, rep.business_id)
    fresh = _build(s, biz, rep.kind, rep.period_start, rep.period_end, rep.payload.get("credit_points_input", 0.0))
    fresh["credit_points_input"] = rep.payload.get("credit_points_input", 0.0)
    if periodic.content_hash(fresh) != rep.content_hash:
        rep.status = "superseded"
        audit(s, actor, "approve_rejected_stale", "report", rep.id)
        s.commit()
        raise Conflict("הנתונים השתנו מאז הכנת הדוח — הדוח סומן כמיושן; יש להכין אותו מחדש")
    rep.status = "approved"
    rep.approved_by = ap.approver_name
    rep.approver_license_type = ap.license_type
    rep.approver_license_number = ap.license_number
    rep.approved_at = utcnow()
    audit(s, actor, "approve", "report", rep.id, approver=ap.approver_name, license_type=ap.license_type,
          license_number=ap.license_number, hash=rep.content_hash)
    s.commit()
    return rep


def transmit_report(s: Session, report_id: int, actor: str) -> None:
    rep = get(s, Report, report_id)
    audit(s, actor, "transmit_attempt", "report", rep.id, status=rep.status)
    s.commit()
    if rep.status != "approved":
        raise Conflict("שידור אפשרי רק לדוח שאושר על ידי מייצג מורשה")
    raise NotImplementedError(
        "שידור ישיר לרשות המסים אינו ממומש: הוא מחייב הרשאת מייצג, הזדהות חזקה ומבנה שידור מאומת. "
        "יש להוריד את הדוח המאושר ולהגישו דרך המייצג."
    )


def unified_file_zip(s: Session, business_id: int, year: int, actor: str) -> bytes:
    biz = get(s, Business, business_id)
    docs = [d for d in periodic._docs(s, biz.id, date(year, 1, 1), date(year, 12, 31)) if d.status == "confirmed"]
    rows = []
    for d in docs:
        e, dec = d.extracted, d.decision
        rows.append({
            "direction": d.direction, "doc_type": e.get("doc_type"), "doc_number": e.get("doc_number"),
            "doc_date": d.doc_date,
            "party_name": e.get("vendor_name") or e.get("customer_name"),
            "party_vat_id": e.get("vendor_id") or e.get("customer_id"),
            "pre_vat": dec.get("pre_vat_amount"), "vat": dec.get("vat_amount"), "total": dec.get("total_amount"),
        })
    audit(s, actor, "export_unified_file", "business", biz.id, year=year, documents=len(rows))
    s.commit()
    return unified_file.build(biz.vat_id, biz.name, rows)


# --- פרטיות (תיקון 13 לחוק הגנת הפרטיות) --------------------------------------
def privacy_export(s: Session, business_id: int, actor: str) -> dict:
    biz = get(s, Business, business_id)
    docs = list_documents(s, biz.id, None)
    reps = list(s.scalars(select(Report).where(Report.business_id == biz.id)))
    logs = list(s.scalars(select(AuditLog).where(AuditLog.entity == "business", AuditLog.entity_id == biz.id)))
    audit(s, actor, "privacy_export", "business", biz.id)
    s.commit()
    return {
        "business": {c.name: getattr(biz, c.name) for c in Business.__table__.columns},
        "documents": [{c.name: getattr(d, c.name) for c in Document.__table__.columns} for d in docs],
        "reports": [{c.name: getattr(r, c.name) for c in Report.__table__.columns} for r in reps],
        "business_audit": [{c.name: getattr(a, c.name) for c in AuditLog.__table__.columns} for a in logs],
    }


def privacy_purge_raw(s: Session, business_id: int, actor: str) -> int:
    """מוחק טקסט גולמי של מסמכים (צמצום מידע). רשומות החשבונאות עצמן נשמרות
    כנדרש בהוראות ניהול פנקסי חשבונות (חובת שמירה) ואינן נמחקות כאן."""
    biz = get(s, Business, business_id)
    n = 0
    for d in list_documents(s, biz.id, None):
        if d.raw_text is not None:
            d.raw_text = None
            n += 1
    audit(s, actor, "privacy_purge_raw", "business", biz.id, purged=n)
    s.commit()
    return n


def bulk_confirm(s: Session, business_id: int, reviewer: str, include_unverified: bool = False) -> dict:
    """אישור מרוכז של מסמכים ממתינים שאין בהם שגיאה ולא דורשים עין אנושית (ראו home.bulk_ready)."""
    from .home import bulk_ready
    confirmed, skipped = [], []
    for doc in list_documents(s, business_id, "pending_review", limit=500):
        ok, why = bulk_ready(doc, include_unverified)
        if not ok:
            skipped.append({"id": doc.id, "reason": why})
            continue
        try:
            review_document(s, doc.id, ReviewIn(action="confirm", reviewer=reviewer), reviewer)
            confirmed.append(doc.id)
        except Conflict as e:  # תקופה נעולה וכד'
            skipped.append({"id": doc.id, "reason": str(e)})
    audit(s, reviewer, "bulk_confirm", "business", business_id, confirmed=len(confirmed), skipped=len(skipped),
          include_unverified=include_unverified)
    s.commit()
    return {"confirmed": confirmed, "skipped": skipped}


def mark_submitted(s: Session, report_id: int, reference: str, actor: str) -> Report:
    """המייצג הגיש בפועל (מחוץ למערכת) — רושמים את מספר האישור שקיבל. זה סוגר את המשימה במסך הבית."""
    rep = get(s, Report, report_id)
    if rep.status != "approved":
        raise Conflict("אפשר לסמן כהוגש רק דוח שאושר על ידי מייצג")
    if rep.submitted_at is not None:
        raise Conflict("הדוח כבר סומן כהוגש")
    rep.submitted_at, rep.submission_ref = utcnow(), reference.strip()[:100]
    audit(s, actor, "mark_submitted", "report", rep.id, reference=rep.submission_ref)
    s.commit()
    return rep
