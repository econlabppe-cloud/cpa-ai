"""מסך הבית: מה צריך מהלקוח עכשיו. מחשב משימות מהנתונים — לא שומר מצב."""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import banking
from .db import Business, Document, Report
from .integrations.addresses import inbound_address
from .reports.periodic import vat_period_bounds
from .rules import data

# ממצאים שמונעים אישור מרוכז: הם דורשים עין אנושית
NOT_BULK_CODES = {"RULE_UNVERIFIED", "FORCED_REVIEW", "CATEGORY_UNKNOWN", "CATEGORY_AMBIGUOUS",
                  "CATEGORY_HINT_CONFLICT", "CATEGORY_FROM_HINT"}


def bulk_ready(doc: Document, include_unverified: bool = False) -> tuple[bool, str]:
    """האם אפשר לאשר מסמך ממתין בלחיצה מרוכזת. מחזיר (כן/לא, סיבה אם לא)."""
    findings = doc.decision.get("findings", [])
    if any(f["severity"] == "error" for f in findings):
        return False, "יש שגיאה במסמך"
    blocking = {f["code"] for f in findings} & NOT_BULK_CODES
    if include_unverified:
        blocking -= {"RULE_UNVERIFIED"}
    if blocking:
        return False, "דורש בדיקה אנושית: " + ", ".join(sorted(blocking))
    return True, ""


HEB_MONTHS = ["ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני", "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר"]


def period_label(start: date, end: date) -> str:
    """'יולי–אוגוסט 2026' — ברור ללקוח יותר מ-07/2026–08/2026."""
    a, b = HEB_MONTHS[start.month - 1], HEB_MONTHS[end.month - 1]
    if (start.year, start.month) == (end.year, end.month):
        return f"{a} {start.year}"
    if start.year == end.year:
        return f"{a}–{b} {start.year}"
    return f"{a} {start.year}–{b} {end.year}"


def _due_date(period_end: date) -> date:
    y, m = (period_end.year + (period_end.month == 12), period_end.month % 12 + 1)
    return date(y, m, min(data.FILING_DAY.value, calendar.monthrange(y, m)[1]))


def last_completed_period(biz: Business, today: date) -> tuple[date, date]:
    """התקופה האחרונה שהסתיימה (זו שצריך לדווח עליה עכשיו)."""
    y, m = today.year, today.month
    while True:
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        start, end = vat_period_bounds(y, m, biz.vat_frequency)
        if end < today.replace(day=1):
            return start, end


def _month_totals(s: Session, business_id: int, start: date, end: date) -> dict:
    income = expenses = 0.0
    n = 0
    for d in s.scalars(select(Document).where(Document.business_id == business_id, Document.status == "confirmed",
                                              Document.doc_date >= start, Document.doc_date <= end)):
        n += 1
        if d.direction == "income":
            sign = -1 if d.extracted.get("doc_type") == "credit_invoice" else 1
            income += sign * d.decision["pre_vat_amount"]
        elif not d.decision.get("is_fixed_asset"):
            expenses += d.decision["recognized_expense"]
    return {"income": round(income, 2), "expenses": round(expenses, 2), "profit": round(income - expenses, 2), "documents": n}


def build_home(s: Session, biz: Business, today: date | None = None) -> dict:
    today = today or date.today()
    month_start = today.replace(day=1)
    month_end = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])

    docs = list(s.scalars(select(Document).where(Document.business_id == biz.id, Document.status != "rejected")))
    pending = [d for d in docs if d.status == "pending_review"]
    ready = [d for d in pending if bulk_ready(d)[0]]
    blocked = [d for d in docs if d.direction == "expense" and any(f["code"] == "ALLOCATION_MISSING" for f in d.decision.get("findings", []))]
    blocked_vat = round(sum((d.decision.get("vat_amount") or 0.0) for d in blocked), 2)

    p_start, p_end = last_completed_period(biz, today)
    due = _due_date(p_end)
    report = s.scalar(select(Report).where(Report.business_id == biz.id, Report.kind == "vat_period",
                                           Report.period_start == p_start, Report.status.in_(("prepared", "approved")))
                      .order_by(Report.id.desc()))
    days_left = (due - today).days
    period_has_docs = any(d.doc_date and p_start <= d.doc_date <= p_end for d in docs)

    bank = banking.unmatched(s, biz.id)
    open_tx = bank["transactions_without_document"]

    tasks: list[dict] = []
    if report is not None and report.status == "approved" and report.submitted_at is None:
        urgency = 1 if days_left <= 7 else 2
        tasks.append({"id": "submit", "priority": urgency, "title": "הדוח אושר — נשאר להגיש לרשות המסים",
                      "detail": ("באיחור של " + str(-days_left) + " ימים" if days_left < 0 else f"עד {due:%d/%m}")
                                + ". אחרי ההגשה רשמו את מספר האישור שקיבלתם",
                      "cta": {"label": "לדוח", "view": "reports"}, "report_id": report.id})
    if report is None or report.status == "prepared":
        # עסק חדש בלי מסמכים בתקופה לא מקבל "באיחור" — אין עדיין על מה לדווח
        if period_has_docs or report is not None:
            urgency = 1 if days_left <= 7 else 3
            label = period_label(p_start, p_end)
            when = f"באיחור של {-days_left} ימים" if days_left < 0 else f"עוד {days_left} ימים (עד {due:%d/%m})"
            tasks.append({"id": "vat_deadline", "priority": urgency,
                          "title": f"דוח מע\"מ — {label}",
                          "detail": when + (" — הדוח מוכן וממתין לאישור המייצג" if report else " — עוד לא הוכן"),
                          "cta": {"label": "לדוח", "view": "reports"}})
    if pending:
        tasks.append({"id": "review", "priority": 2,
                      "title": "מסמך אחד מחכה לבדיקה שלך" if len(pending) == 1 else f"{len(pending)} מסמכים מחכים לבדיקה שלך",
                      "detail": f"{len(ready)} מהם בטוחים לאישור בלחיצה אחת" if ready else "כל אחד דורש הסתכלות קצרה",
                      "cta": {"label": "לבדיקה", "view": "docs"}, "bulk_ready": len(ready)})
    if blocked_vat > 0:
        vendors = sorted({d.extracted.get("vendor_name") or "ספק" for d in blocked})[:3]
        tasks.append({"id": "allocation", "priority": 2, "title": f"₪{blocked_vat:,.0f} מע\"מ חסום — חסר מספר הקצאה",
                      "detail": "לבקש חשבונית מעודכנת מ: " + ", ".join(vendors),
                      "cta": {"label": "לפרטים", "view": "docs"}})
    if open_tx:
        total = sum(abs(t["amount"]) for t in open_tx)
        tasks.append({"id": "bank", "priority": 3,
                      "title": (f"חיוב אחד בבנק בלי קבלה (₪{total:,.0f})" if len(open_tx) == 1
                                else f"{len(open_tx)} חיובים בבנק בלי קבלה (₪{total:,.0f})"),
                      "detail": "לשלוח את הקבלות, או לסמן שזו הוצאה פרטית",
                      "cta": {"label": "לבנק", "view": "bank"}})
    if not docs:
        tasks.append({"id": "first_doc", "priority": 4, "title": "העלו את החשבונית הראשונה",
                      "detail": "גררו קובץ, העבירו במייל או שלחו בווטסאפ", "cta": {"label": "להעלאה", "view": "home"}})
    if not inbound_address(biz):
        tasks.append({"id": "setup_email", "priority": 4, "title": "חברו מייל כדי שחשבוניות ייקלטו לבד",
                      "detail": "הגדרה חד-פעמית בשרת", "cta": {"label": "להגדרות", "view": "settings"}})
    if not biz.whatsapp_phone:
        tasks.append({"id": "setup_whatsapp", "priority": 4, "title": "חברו את הווטסאפ שלכם",
                      "detail": "אפשר יהיה לשלוח קבלה בתמונה ולקבל תשובה מיד",
                      "cta": {"label": "להגדרות", "view": "settings"}})
    if biz.advance_rate_percent == 0 and any(d.direction == "income" for d in docs):
        tasks.append({"id": "advance_rate", "priority": 4, "title": "הגדירו שיעור מקדמות",
                      "detail": "בלי זה דוח המקדמות יהיה 0 ₪", "cta": {"label": "להגדרות", "view": "settings"}})
    tasks.sort(key=lambda t: t["priority"])

    next_start = p_end + timedelta(days=1)
    n_start, n_end = vat_period_bounds(next_start.year, next_start.month, biz.vat_frequency)
    n_due = _due_date(n_end)
    deadlines = [
        {"kind": "vat", "label": "דוח מע\"מ", "period_start": p_start.isoformat(), "period_end": p_end.isoformat(),
         "period_label": period_label(p_start, p_end), "period_open": False, "due": due.isoformat(), "days_left": days_left,
         "status": ("submitted" if report and report.submitted_at else report.status) if report else "none",
         "submission_ref": report.submission_ref if report else None},
        # אחרי שהתקופה האחרונה הוגשה, זה המועד שהלקוח צריך לראות
        {"kind": "vat", "label": "דוח מע\"מ", "period_start": n_start.isoformat(), "period_end": n_end.isoformat(),
         "period_label": period_label(n_start, n_end), "period_open": n_end >= today, "due": n_due.isoformat(),
         "days_left": (n_due - today).days,
         "status": "none", "submission_ref": None},
    ]

    return {
        "today": today.isoformat(),
        "month": _month_totals(s, biz.id, month_start, month_end),
        "year": _month_totals(s, biz.id, date(today.year, 1, 1), date(today.year, 12, 31)),
        "tasks": tasks,
        "all_clear": not any(t["priority"] <= 3 for t in tasks),
        "deadlines": deadlines,
        "deadline_note": {"verified": data.FILING_DAY.source.verified, "text": data.FILING_DAY.source.note},
        "counts": {"pending": len(pending), "bulk_ready": len(ready), "documents": len(docs),
                   "bank_open": len(open_tx)},
    }
