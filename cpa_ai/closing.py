"""צ'קליסט סגירת שנה: מה נשאר לעשות כדי שהספרים של השנה יהיו מוכנים להגשה."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import banking, collab, health, insights
from .db import BankTransaction, Business, Document, Report
from .home import period_label
from .reports.periodic import vat_period_bounds


def _status(s: Session, biz_id: int, kind: str, start: date) -> str:
    rep = s.scalars(select(Report).where(
        Report.business_id == biz_id, Report.kind == kind, Report.period_start == start,
        Report.status.in_(("prepared", "approved"))).order_by(Report.id.desc())).first()
    if rep is None:
        return "none"
    if rep.status == "approved":
        return "submitted" if rep.submitted_at else "approved"
    return "prepared"


def _periods_item(s: Session, biz: Business, year: int, today: date, kind: str, item_id: str, title: str) -> dict:
    wait, seen = [], set()
    for m in range(1, 13):
        start, end = vat_period_bounds(year, m, biz.vat_frequency)
        if start in seen or end >= today:
            continue
        seen.add(start)
        st = _status(s, biz.id, kind, start)
        if st != "submitted":
            wait.append(f"{period_label(start, end)} ({ {'none': 'לא הוכן', 'prepared': 'מחכה לאישור', 'approved': 'לא סומן כהוגש'}[st] })")
    return {"id": item_id, "title": title, "status": "done" if not wait else "todo",
            "detail": "כל התקופות שהסתיימו הוגשו" if not wait else "; ".join(wait),
            "cta": {"view": "reports", "label": "לדוחות"}}


def year_close(s: Session, biz: Business, year: int, today: date | None = None) -> dict:
    today = today or date.today()
    docs = list(s.scalars(select(Document).where(
        Document.business_id == biz.id, Document.status != "rejected",
        Document.doc_date >= date(year, 1, 1), Document.doc_date <= date(year, 12, 31))))
    pending = [d for d in docs if d.status == "pending_review"]
    items = [{"id": "pending", "title": "כל המסמכים נבדקו", "status": "done" if not pending else "todo",
              "detail": "אין מסמכים ממתינים" if not pending else f"{len(pending)} מסמכים ממתינים לבדיקה",
              "cta": {"view": "docs", "label": "לבדיקה"}},
             _periods_item(s, biz, year, today, "vat_period", "periods_vat", "דוחות מע\"מ הוגשו")]
    if biz.advance_rate_percent > 0:
        items.append(_periods_item(s, biz, year, today, "income_tax_advance", "periods_advance", "דוחות מקדמות הוגשו"))

    un = banking.unmatched(s, biz.id)
    has_tx = s.scalar(select(BankTransaction.id).where(BankTransaction.business_id == biz.id).limit(1)) is not None
    if not has_tx:
        items.append({"id": "bank", "title": "תנועות בנק ואשראי יובאו", "status": "todo",
                      "detail": "לא יובאו תנועות — בלעדיהן אי אפשר לדעת איזה חיוב חסרה לו קבלה",
                      "cta": {"view": "bank", "label": "לבנק"}})
    else:
        n = len(un["transactions_without_document"])
        items.append({"id": "bank", "title": "כל חיוב בבנק מתועד", "status": "done" if not n else "todo",
                      "detail": "אין חיובים בלי קבלה" if not n else f"{n} חיובים בלי קבלה או חשבונית",
                      "cta": {"view": "bank", "label": "לבנק"}})

    h = health.books_health(s, biz, year, today)
    high = [i for i in h["issues"] if i["severity"] == "high"]
    items.append({"id": "health", "title": "אין ממצאים חמורים בספרים", "status": "done" if not high else "todo",
                  "detail": f"מדד בריאות {h['score']}" + ("" if not high else ": " + "; ".join(i["title"] for i in high)),
                  "cta": {"view": "more", "label": "לבדיקת הספרים", "anchor": "m-health"}})

    ar = insights.receivables(s, biz, today)
    old = ar["buckets"]["90+"]
    items.append({"id": "receivables", "title": "אין חובות לקוחות ישנים", "status": "done" if old == 0 else "todo",
                  "detail": "אין חשבוניות פתוחות מעל 90 יום" if old == 0 else f"₪{old:,.0f} פתוחים מעל 90 יום — לגבות או לשקול חוב מסופק",
                  "cta": {"view": "more", "label": "לגבייה", "anchor": "m-ar"}})

    est = _status(s, biz.id, "annual_estimate", date(year, 1, 1))
    items.append({"id": "annual_estimate", "title": "הערכה שנתית הוכנה ואושרה",
                  "status": "done" if est in ("approved", "submitted") else "todo",
                  "detail": {"none": "עוד לא הוכנה", "prepared": "הוכנה, מחכה לאישור המייצג"}.get(est, "אושרה"),
                  "cta": {"view": "reports", "label": "לדוחות"}})

    th = collab.counts(s, biz.id)
    items.append({"id": "threads", "title": "אין פניות פתוחות מול הלקוח",
                  "status": "done" if th["waiting_on_client"] + th["waiting_on_accountant"] == 0 else "todo",
                  "detail": f"{th['waiting_on_client']} מחכות ללקוח · {th['waiting_on_accountant']} לרו\"ח",
                  "cta": {"view": "threads", "label": "לפניות"}})

    wh = sum((d.extracted.get("withholding_tax") or 0) for d in docs if d.direction == "income" and d.status == "confirmed")
    manual = []
    if wh > 0:
        manual.append({"id": "withholding_certs", "title": "אישורי ניכוי במקור התקבלו מהלקוחות", "status": "manual",
                       "detail": f"נוכו ₪{wh:,.0f} — לבקש אישור שנתי מכל לקוח ולשמור", "cta": None})
    manual.append({"id": "backup", "title": "גיבוי של בסיס הנתונים וקבצי המקור", "status": "manual",
                   "detail": "python -m cpa_ai backup out.zip — ולבדוק עם verify-backup. מסמכי מקור נשמרים 7 שנים", "cta": None})
    manual.append({"id": "assets", "title": "ציוד שנרכש השנה נרשם כרכוש קבוע", "status": "manual",
                   "detail": "מסמכים בקטגוריית ציוד נכנסים ללוח הפחת אוטומטית; לוודא שלא חסר נכס שנרכש בלי חשבונית", "cta": None})
    items += manual
    counted = [i for i in items if i["status"] in ("done", "todo")]
    done = sum(i["status"] == "done" for i in counted)
    return {"year": year, "percent": round(100 * done / len(counted)) if counted else 100, "items": items,
            "disclaimer": "הצ'קליסט מבוסס על מה שרשום במערכת; אינו מחליף בדיקה של המייצג."}
