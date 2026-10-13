"""תמונת מצב עסקית מעבר לדוחות החובה: גיול לקוחות, רווח והפסד חודשי, וחשבוניות חוזרות שחסרות.

הכל נגזר ממסמכים שאושרו. אין כאן החלטות מס — רק מידע שעוזר לבעל העסק לגבות, לתכנן ולא לפספס הוצאה.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import BankTransaction, Business, Document

BUCKETS = (("0-30", 30), ("31-60", 60), ("61-90", 90), ("90+", None))


def _bucket(age: int) -> str:
    for name, limit in BUCKETS:
        if limit is None or age <= limit:
            return name
    return BUCKETS[-1][0]


def receivables(s: Session, biz: Business, today: date | None = None) -> dict:
    """חשבוניות שהוצאו ללקוחות ועוד לא נמצא להן תשלום (התאמת בנק או סימון ידני)."""
    today = today or date.today()
    txs = list(s.scalars(select(BankTransaction).where(BankTransaction.business_id == biz.id)))
    paid_by_bank = {t.matched_document_id for t in txs if t.matched_document_id}
    items, buckets = [], {name: 0.0 for name, _ in BUCKETS}
    for d in s.scalars(select(Document).where(
            Document.business_id == biz.id, Document.direction == "income", Document.status == "confirmed",
            Document.doc_date.is_not(None)).order_by(Document.doc_date)):
        if d.extracted.get("doc_type") == "credit_invoice" or d.paid_on or d.id in paid_by_bank:
            continue
        # הלקוח מעביר את הסכום פחות הניכוי במקור
        due = round((d.decision.get("total_amount") or 0.0) - (d.extracted.get("withholding_tax") or 0.0), 2)
        if due <= 0:
            continue
        age = (today - d.doc_date).days
        b = _bucket(max(age, 0))
        buckets[b] = round(buckets[b] + due, 2)
        items.append({"document_id": d.id, "customer": d.extracted.get("customer_name"),
                      "doc_number": d.extracted.get("doc_number"), "date": d.doc_date.isoformat(),
                      "amount_due": due, "age_days": age, "bucket": b})
    return {
        "today": today.isoformat(),
        "items": items,
        "buckets": buckets,
        "total_open": round(sum(buckets.values()), 2),
        "bank_imported": bool(txs),
        "note": ("לא יובא דף בנק — כל חשבונית שלא סומנה כשולמה מופיעה כפתוחה" if not txs else
                 "חשבונית נחשבת משולמת אם הותאמה לזיכוי בבנק או סומנה ידנית"),
    }


def monthly_pnl(s: Session, biz: Business, year: int) -> dict:
    """הכנסות והוצאות מוכרות לפי חודש (לפי תאריך המסמך). פחת שנתי אינו מפוזר לחודשים."""
    months = [{"month": f"{year}-{m:02d}", "income": 0.0, "expenses": 0.0, "fixed_assets": 0.0, "profit": 0.0}
              for m in range(1, 13)]
    for d in s.scalars(select(Document).where(
            Document.business_id == biz.id, Document.status == "confirmed",
            Document.doc_date >= date(year, 1, 1), Document.doc_date <= date(year, 12, 31))):
        row = months[d.doc_date.month - 1]
        if d.direction == "income":
            sign = -1 if d.extracted.get("doc_type") == "credit_invoice" else 1
            row["income"] += sign * (d.decision.get("pre_vat_amount") or 0.0)
        elif d.decision.get("is_fixed_asset"):
            row["fixed_assets"] += d.decision.get("recognized_expense") or 0.0
        else:
            row["expenses"] += d.decision.get("recognized_expense") or 0.0
    for row in months:
        for k in ("income", "expenses", "fixed_assets"):
            row[k] = round(row[k], 2)
        row["profit"] = round(row["income"] - row["expenses"], 2)
    total = {k: round(sum(r[k] for r in months), 2) for k in ("income", "expenses", "fixed_assets", "profit")}
    return {"year": year, "months": months, "total": total,
            "note": "רכוש קבוע מוצג בנפרד ואינו מנוכה ברווח החודשי — הוא מוכר דרך פחת בהערכה השנתית"}


def _month_back(d: date, n: int) -> tuple[int, int]:
    m = d.year * 12 + d.month - 1 - n
    return m // 12, m % 12 + 1


def missing_recurring(s: Session, biz: Business, today: date | None = None, history: int = 3) -> list[dict]:
    """ספק שהגיש חשבונית בכל אחד מ-`history` החודשים שלפני החודש הקודם, אבל לא בחודש הקודם —
    כנראה חשבונית שלא הגיעה (חשמל, תקשורת, שכירות). הוצאה שלא נקלטה = מס ומע"מ ששולמו ביותר."""
    today = today or date.today()
    target = _month_back(today, 1)
    wanted = [_month_back(today, k) for k in range(2, 2 + history)]
    start = date(*wanted[-1], 1)
    end = date(target[0], target[1], calendar.monthrange(*target)[1])
    seen: dict[str, set] = defaultdict(set)
    names: dict[str, str] = {}
    totals: dict[str, list[float]] = defaultdict(list)
    for d in s.scalars(select(Document).where(
            Document.business_id == biz.id, Document.direction == "expense", Document.status != "rejected",
            Document.party_id.is_not(None), Document.doc_date >= start, Document.doc_date <= end)):
        seen[d.party_id].add((d.doc_date.year, d.doc_date.month))
        names[d.party_id] = d.extracted.get("vendor_name") or d.party_id
        if d.decision.get("total_amount"):
            totals[d.party_id].append(d.decision["total_amount"])
    out = []
    for pid, months in seen.items():
        if all(m in months for m in wanted) and target not in months:
            vals = sorted(totals[pid])
            out.append({"party_id": pid, "vendor": names[pid], "month": f"{target[0]}-{target[1]:02d}",
                        "typical_total": vals[len(vals) // 2] if vals else None})
    return sorted(out, key=lambda x: x["vendor"])
