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


def parties(s: Session, biz: Business, year: int) -> dict:
    """סיכום לפי ספק ולפי לקוח בשנה: כמה מסמכים, כמה כסף, איזה חלק מהסך. לעיון, לבדיקת ריכוזיות ולחיפוש."""
    vend: dict[str, dict] = {}
    cust: dict[str, dict] = {}
    for d in s.scalars(select(Document).where(
            Document.business_id == biz.id, Document.status == "confirmed",
            Document.doc_date >= date(year, 1, 1), Document.doc_date <= date(year, 12, 31))):
        sign = -1 if d.extracted.get("doc_type") == "credit_invoice" else 1
        total = sign * (d.decision.get("total_amount") or 0.0)
        if d.direction == "income":
            key, name, bucket = d.extracted.get("customer_id") or d.extracted.get("customer_name") or "?", \
                d.extracted.get("customer_name") or "לקוח", cust
        else:
            key, name, bucket = d.party_id or d.extracted.get("vendor_name") or "?", \
                d.extracted.get("vendor_name") or "ספק", vend
        row = bucket.setdefault(key, {"party_id": d.party_id, "name": name, "documents": 0, "total": 0.0,
                                      "categories": set()})
        row["documents"] += 1
        row["total"] += total
        if d.direction == "expense" and d.decision.get("category_label"):
            row["categories"].add(d.decision["category_label"])

    def finish(bucket: dict) -> list[dict]:
        grand = sum(r["total"] for r in bucket.values()) or 0.0
        rows = sorted(bucket.values(), key=lambda r: -r["total"])
        return [{**r, "total": round(r["total"], 2), "categories": sorted(r["categories"]),
                 "share": round(r["total"] / grand, 4) if grand > 0 else 0.0} for r in rows]
    return {"year": year, "vendors": finish(vend), "customers": finish(cust)}


def avg_collection_days(s: Session, biz: Business) -> int:
    """כמה ימים בממוצע עד שלקוחות של העסק משלמים — לפי חשבוניות ששולמו (סימון ידני או התאמת בנק). ברירת מחדל 30."""
    paid: dict[int, date] = {}
    for t in s.scalars(select(BankTransaction).where(
            BankTransaction.business_id == biz.id, BankTransaction.matched_document_id.is_not(None))):
        paid[t.matched_document_id] = t.tx_date
    days = []
    for d in s.scalars(select(Document).where(
            Document.business_id == biz.id, Document.direction == "income", Document.status == "confirmed",
            Document.doc_date.is_not(None))):
        when = d.paid_on or paid.get(d.id)
        if when and d.extracted.get("doc_type") != "credit_invoice":
            days.append(max((when - d.doc_date).days, 0))
    if not days:
        return 30
    return min(max(round(sum(days) / len(days)), 0), 120)


def _recurring(s: Session, biz: Business, today: date, history: int = 3) -> list[dict]:
    """ספקים שהגישו חשבונית בכל אחד מ-`history` החודשים האחרונים שהסתיימו."""
    wanted = [_month_back(today, k) for k in range(1, 1 + history)]
    start = date(*wanted[-1], 1)
    seen: dict[str, set] = defaultdict(set)
    names: dict[str, str] = {}
    totals: dict[str, list[float]] = defaultdict(list)
    current: set[str] = set()
    for d in s.scalars(select(Document).where(
            Document.business_id == biz.id, Document.direction == "expense", Document.status != "rejected",
            Document.party_id.is_not(None), Document.doc_date >= start)):
        if d.extracted.get("doc_type") == "credit_invoice":
            continue
        ym = (d.doc_date.year, d.doc_date.month)
        seen[d.party_id].add(ym)
        names[d.party_id] = d.extracted.get("vendor_name") or d.party_id
        if ym in wanted and d.decision.get("total_amount"):
            totals[d.party_id].append(d.decision["total_amount"])
        if ym == (today.year, today.month):
            current.add(d.party_id)
    out = []
    for pid, months in seen.items():
        if all(m in months for m in wanted) and totals[pid]:
            vals = sorted(totals[pid])
            out.append({"party_id": pid, "vendor": names[pid], "typical": vals[len(vals) // 2],
                        "billed_this_month": pid in current})
    return sorted(out, key=lambda r: r["vendor"])


def cashflow(s: Session, biz: Business, today: date | None = None, months: int = 3) -> dict:
    """תחזית תזרים לחודשים הקרובים מהנתונים שיש במערכת: גבייה צפויה (לפי ההרגלים של הלקוחות),
    הוצאות קבועות, ותשלומי מע"מ ומקדמות לפי הדוחות. אין כאן יתרת בנק — רק תנועה נטו."""
    from datetime import timedelta

    from .db import Report
    from .home import _due_date, last_completed_period
    from .reports import periodic
    today = today or date.today()
    horizon = [(today.year + (today.month - 1 + k) // 12, (today.month - 1 + k) % 12 + 1) for k in range(months)]
    rows = {ym: {"month": f"{ym[0]}-{ym[1]:02d}", "inflow": 0.0, "recurring": 0.0, "vat": 0.0, "advance": 0.0,
                 "items": []} for ym in horizon}
    later = 0.0

    def place(when: date, field: str, amount: float, kind: str, label: str):
        nonlocal later
        when = max(when, today)
        ym = (when.year, when.month)
        if ym not in rows:
            if field == "inflow":
                later += amount
            return
        rows[ym][field] += amount
        rows[ym]["items"].append({"kind": kind, "label": label, "amount": round(amount, 2), "date": when.isoformat()})

    avg = avg_collection_days(s, biz)
    ar = receivables(s, biz, today)
    for it in ar["items"]:
        due = date.fromisoformat(it["date"]) + timedelta(days=avg)
        overdue = due < today
        place(today + timedelta(days=7) if overdue else due, "inflow", it["amount_due"], "receivable",
              f"{it['customer'] or 'לקוח'}" + (" (באיחור)" if overdue else ""))

    for r in _recurring(s, biz, today):
        for k, ym in enumerate(horizon):
            if k == 0 and r["billed_this_month"]:
                continue
            rows[ym]["recurring"] += r["typical"]
            rows[ym]["items"].append({"kind": "recurring", "label": r["vendor"], "amount": round(r["typical"], 2),
                                      "date": None})

    p_start, p_end = last_completed_period(biz, today)
    cur_start, cur_end = periodic.vat_period_bounds(today.year, today.month, biz.vat_frequency)
    for start, end, tag in ((p_start, p_end, "אחרון שהסתיים"), (cur_start, cur_end, "תקופה נוכחית עד היום")):
        done = s.scalar(select(Report.id).where(
            Report.business_id == biz.id, Report.kind == "vat_period", Report.period_start == start,
            Report.status == "approved", Report.submitted_at.is_not(None)))
        if done:
            continue
        due = _due_date(end)
        vat = periodic.build_vat_period(s, biz, start, end)["vat_payable"]
        if vat > 0:
            place(due, "vat", vat, "vat", f"מע\"מ — {tag}")
        elif vat < 0:
            place(due, "inflow", -vat, "vat_refund", f"החזר מע\"מ — {tag}")
        if biz.advance_rate_percent > 0:
            adv = periodic.build_income_tax_advance(s, biz, start, end)["advance_payable"]
            if adv > 0:
                place(due, "advance", adv, "advance", f"מקדמת מס הכנסה — {tag}")

    out = []
    for ym in horizon:
        r = rows[ym]
        r.update({k: round(r[k], 2) for k in ("inflow", "recurring", "vat", "advance")})
        r["net"] = round(r["inflow"] - r["recurring"] - r["vat"] - r["advance"], 2)
        out.append(r)
    return {
        "today": today.isoformat(), "months": out, "later_inflow": round(later, 2), "collection_days": avg,
        "assumptions": [
            f"גבייה: {avg} ימים מתאריך החשבונית (ממוצע לפי הלקוחות של העסק; 30 אם אין עדיין נתונים). חשבונית באיחור צפויה בעוד שבוע",
            "הוצאות קבועות: ספקים שחייבו בכל אחד משלושת החודשים האחרונים, לפי הסכום החציוני",
            "מע\"מ ומקדמות לפי הדוחות שהמערכת מחשבת, ועד מועד ההגשה שמוגדר בה (לא אומת)",
            "לא כלולים: יתרת הבנק, שכר, ביטוח לאומי, הוצאות חד-פעמיות, ומע\"מ ומקדמות של תקופות עתידיות",
        ],
        "notes": [],
    }
