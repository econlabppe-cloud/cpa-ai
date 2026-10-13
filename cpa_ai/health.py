"""בדיקת בריאות הספרים: מה בספרים עלול לעורר שאלות בביקורת, ומה כדאי לתקן לפני שמגישים.

שני סוגי בדיקות:
- **דטרמיניסטיות** (פער במספור חשבוניות, מספר כפול, תאריך עתידי): ניהול ספרים תקין מחייב מספור רציף.
- **אינדיקציות** (תלות בלקוח אחד, שיעור מזומן, הפסד שוטף): סף היוריסטי שלנו, **לא כלל חוק** ולא ניבוי של ביקורת.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import banking, insights
from .db import Business, Document

WEIGHTS = {"high": 15, "medium": 7, "low": 2}
CASH_SHARE_LIMIT = 0.10
DEPENDENCE_SHARE = 0.70
STALE_PENDING_DAYS = 14
MAX_GAP_SPAN = 5000


def _issue(code, severity, title, detail, action, doc_ids=None, data=None) -> dict:
    return {"code": code, "severity": severity, "title": title, "detail": detail, "action": action,
            "document_ids": doc_ids or [], "detail_data": data or {}}


def _num(raw) -> int | None:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return int(digits) if digits and len(digits) <= 9 else None


def books_health(s: Session, biz: Business, year: int, today: date | None = None) -> dict:
    today = today or date.today()
    lo, hi = date(year, 1, 1), date(year, 12, 31)
    docs = list(s.scalars(select(Document).where(
        Document.business_id == biz.id, Document.status != "rejected",
        Document.doc_date >= lo, Document.doc_date <= hi)))
    issues: list[dict] = []

    income = [d for d in docs if d.direction == "income" and d.status == "confirmed"]
    expenses = [d for d in docs if d.direction == "expense" and d.status == "confirmed"]

    # --- מספור חשבוניות הכנסה: רציף, ייחודי, קיים (לכל סוג מסמך בנפרד) ---
    by_type: dict[str, list[tuple[int, Document]]] = defaultdict(list)
    no_number = []
    for d in income:
        n = _num(d.extracted.get("doc_number"))
        if n is None:
            no_number.append(d)
        else:
            by_type[d.extracted.get("doc_type", "tax_invoice")].append((n, d))
    for doc_type, rows in by_type.items():
        nums = [n for n, _ in rows]
        dup = sorted({n for n in nums if nums.count(n) > 1})
        if dup:
            issues.append(_issue(
                "INCOME_NUMBER_DUPLICATE", "high", "מספר חשבונית כפול",
                f"אותו מספר הופיע יותר מפעם אחת: {', '.join(map(str, dup[:10]))}",
                "לוודא שלא הונפקו שתי חשבוניות עם אותו מספר; חשבונית מס ממוספרת פעם אחת בלבד",
                [d.id for n, d in rows if n in dup], {"numbers": dup, "doc_type": doc_type}))
        uniq = sorted(set(nums))
        if len(uniq) >= 2 and uniq[-1] - uniq[0] <= MAX_GAP_SPAN:
            present = set(uniq)
            missing = [n for n in range(uniq[0], uniq[-1] + 1) if n not in present]
            if missing:
                issues.append(_issue(
                    "INCOME_NUMBER_GAP", "high", "פער במספור חשבוניות",
                    f"חסרים המספרים {', '.join(map(str, missing[:15]))}{'…' if len(missing) > 15 else ''}",
                    "ניהול ספרים תקין מחייב מספור רציף. לבדוק אם חשבוניות אלה בוטלו (מסמך ביטול נדרש) "
                    "או לא נקלטו כאן",
                    [d.id for _, d in rows], {"missing": missing[:200], "doc_type": doc_type}))
    if no_number:
        issues.append(_issue(
            "INCOME_NUMBER_MISSING", "medium", "הכנסות ללא מספר חשבונית",
            f"{len(no_number)} חשבוניות הכנסה נרשמו בלי מספר מסמך", "להשלים מספר חשבונית מהמסמך המקורי",
            [d.id for d in no_number]))

    # --- תאריכים ותור בדיקה ---
    future = [d for d in docs if d.doc_date > today + timedelta(days=1)]
    if future:
        issues.append(_issue("FUTURE_DATE", "high", "מסמכים עם תאריך עתידי",
                             f"{len(future)} מסמכים מתוארכים אחרי היום — לרוב טעות בקריאה או בהקלדה",
                             "לתקן את התאריך", [d.id for d in future]))
    stale = [d for d in docs if d.status == "pending_review" and d.created_at
             and d.created_at.date() < today - timedelta(days=STALE_PENDING_DAYS)]
    if stale:
        issues.append(_issue("STALE_PENDING", "medium", "מסמכים שממתינים יותר משבועיים",
                             f"{len(stale)} מסמכים ממתינים לבדיקה מעל {STALE_PENDING_DAYS} ימים",
                             "לאשר, לתקן או לדחות — מסמך תקוע מעכב הגשת דוח", [d.id for d in stale]))

    # --- ניכוי במקור ---
    wh_docs = [d for d in income if (d.extracted.get("withholding_tax") or 0) > 0]
    if wh_docs:
        total = round(sum(d.extracted["withholding_tax"] for d in wh_docs), 2)
        customers = sorted({d.extracted.get("customer_name") or "לקוח" for d in wh_docs})
        issues.append(_issue(
            "WITHHOLDING_CERTIFICATES", "medium", "אישורי ניכוי מס במקור",
            f"נוכו {total:,.0f} ₪ על ידי {len(customers)} לקוחות", "לבקש מכל לקוח אישור שנתי על הסכום שנוכה "
            "ולהחזיק אותו — בלי האישור הזיכוי עלול להישלל", [d.id for d in wh_docs],
            {"total": total, "customers": customers}))

    # --- אינדיקציות ---
    total_exp = sum(abs(d.decision.get("recognized_expense") or 0) for d in expenses) or 0.0
    cash = [d for d in expenses if d.decision.get("payment_method") == "cash"]
    if len(cash) >= 2 and total_exp:
        share = sum(abs(d.decision.get("total_amount") or 0) for d in cash) / max(
            sum(abs(d.decision.get("total_amount") or 0) for d in expenses), 1)
        if share > CASH_SHARE_LIMIT:
            issues.append(_issue("CASH_SHARE", "medium", "שיעור גבוה של הוצאות במזומן",
                                 f"{share:.0%} מההוצאות שולמו במזומן ({len(cash)} מסמכים)",
                                 "מומלץ לעבור לתשלום באשראי או בהעברה — מזומן קשה יותר לתעד, ויש תקרה על תשלום בין עסקים",
                                 [d.id for d in cash], {"share": round(share, 4)}))
    pnl = insights.monthly_pnl(s, biz, year)["total"]
    if pnl["income"] > 0 and pnl["profit"] < 0:
        issues.append(_issue("LOSS_YEAR", "medium", "הפסד שוטף",
                             f"ההוצאות עולות על ההכנסות ב-{-pnl['profit']:,.0f} ₪",
                             "הפסד חוזר מעורר שאלות בשומה: לוודא שכל ההוצאות מתועדות ועסקיות, ולשמור הסבר (השקעה, שנת פתיחה)",
                             data={"profit": pnl["profit"]}))
    by_customer: dict[str, float] = defaultdict(float)
    for d in income:
        sign = -1 if d.extracted.get("doc_type") == "credit_invoice" else 1
        by_customer[d.extracted.get("customer_name") or "לקוח"] += sign * (d.decision.get("pre_vat_amount") or 0)
    total_inc = sum(by_customer.values())
    if len(income) >= 3 and total_inc > 0:
        top, amt = max(by_customer.items(), key=lambda kv: kv[1])
        if amt / total_inc > DEPENDENCE_SHARE:
            issues.append(_issue("CUSTOMER_DEPENDENCE", "low", "תלות בלקוח אחד",
                                 f"{top}: {amt / total_inc:.0%} מההכנסות",
                                 "תלות גבוהה בלקוח אחד עלולה להיחשב יחסי עובד–מעביד ולהיות גם סיכון עסקי; "
                                 "כדאי להבין זאת עם המייצג",
                                 data={"customer": top, "share": round(amt / total_inc, 4)}))
    no_file = [d for d in expenses if not d.attachment_path]
    if no_file:
        issues.append(_issue("NO_SOURCE_FILE", "low", "הוצאות בלי קובץ מקור",
                             f"{len(no_file)} הוצאות נקלטו מטקסט בלבד — אין חשבונית מקורית שמורה",
                             "חובה לשמור את המסמך המקורי (7 שנים). להעלות את הקובץ או לצלם",
                             [d.id for d in no_file]))
    un = banking.unmatched(s, biz.id)
    if un["transactions_without_document"]:
        n = len(un["transactions_without_document"])
        issues.append(_issue("UNMATCHED_BANK", "medium", "חיובים בבנק בלי מסמך",
                             f"{n} חיובים ללא קבלה או חשבונית",
                             "לבקש את המסמכים מהלקוח (בלשונית פניות) או לסמן הוצאה פרטית"))

    score = max(0, 100 - sum(WEIGHTS[i["severity"]] for i in issues))
    order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda i: order[i["severity"]])
    return {
        "year": year, "score": score, "issues": issues,
        "stats": {"income_documents": len(income), "expense_documents": len(expenses)},
        "disclaimer": ("מספור, כפילות ותאריכים הם בדיקות דטרמיניסטיות. כל השאר הם אינדיקציות לבדיקה לפי סף שהגדרנו — "
                       "אינן כללי חוק ואינן ניבוי של ביקורת או שומה."),
    }
