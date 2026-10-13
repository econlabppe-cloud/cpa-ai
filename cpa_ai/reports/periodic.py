"""הכנת דוחות: מע"מ תקופתי, מקדמות מס הכנסה, והערכה שנתית.

הדוחות מוכנים כטיוטה בלבד. הגשה מחייבת אישור של מייצג מורשה (ראו workflow.py).
מספרי טפסים רשמיים אינם מקודדים כאן עד לאימותם.
"""
from __future__ import annotations

import calendar
import hashlib
import json
from collections import defaultdict
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import Business, Document, Report
from ..rules import data


def r2(x: float) -> float:
    return round(x + 0.0, 2)


def content_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def vat_period_bounds(year: int, month: int, frequency: str) -> tuple[date, date]:
    if not 1 <= month <= 12:
        raise ValueError("חודש לא תקין")
    if frequency == "bimonthly":
        first = month if month % 2 == 1 else month - 1
        last = first + 1
    else:
        first = last = month
    return date(year, first, 1), date(year, last, calendar.monthrange(year, last)[1])


def _docs(s: Session, business_id: int, start: date, end: date, by_period: bool = False) -> list[Document]:
    """by_period: לפי תקופת הדיווח (מע"מ ומקדמות) — מסמך מאוחר נכלל בתקופה שאליה שויך.
    בלי: לפי תאריך המסמך (הערכה שנתית — ההכנסה וההוצאה שייכות לשנת המסמך)."""
    when = func.coalesce(Document.period_date, Document.doc_date) if by_period else Document.doc_date
    return list(s.scalars(
        select(Document).where(
            Document.business_id == business_id,
            when >= start,
            when <= end,
            Document.status != "rejected",
        ).order_by(Document.doc_date, Document.id)
    ))


def _pending(docs: list[Document]) -> list[int]:
    return [d.id for d in docs if d.status == "pending_review"]


def _undated_pending(s: Session, business_id: int) -> int:
    return len(list(s.scalars(select(Document.id).where(
        Document.business_id == business_id, Document.doc_date.is_(None), Document.status == "pending_review"))))


def build_vat_period(s: Session, biz: Business, start: date, end: date) -> dict:
    docs = _docs(s, biz.id, start, end, by_period=True)
    confirmed = [d for d in docs if d.status == "confirmed"]
    sales_pre = sales_vat = eq_vat = other_vat = 0.0
    for d in confirmed:
        dec = d.decision
        if d.direction == "income":
            sign = -1 if d.extracted.get("doc_type") == "credit_invoice" else 1
            sales_pre += sign * dec["pre_vat_amount"]
            sales_vat += sign * dec["vat_amount"]
        elif dec.get("is_fixed_asset"):
            eq_vat += dec["deductible_vat"]
        else:
            other_vat += dec["deductible_vat"]

    warnings = []
    pending = _pending(docs)
    if pending:
        warnings.append(f"{len(pending)} מסמכים בתקופה ממתינים לבדיקה ולא נכללו — לא ניתן לאשר עד לטיפולם")
    undated = _undated_pending(s, biz.id)
    if undated:
        warnings.append(f"{undated} מסמכים ללא תאריך ממתינים לבדיקה — ייתכן שהם שייכים לתקופה")

    return {
        "kind": "vat_period",
        "business": {"name": biz.name, "vat_id": biz.vat_id, "frequency": biz.vat_frequency},
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "sales": {"taxable_pre_vat": r2(sales_pre), "output_vat": r2(sales_vat)},
        "inputs": {"fixed_assets_vat": r2(eq_vat), "other_vat": r2(other_vat)},
        "vat_payable": r2(sales_vat - eq_vat - other_vat),
        "included_document_ids": [d.id for d in confirmed],
        "pending_document_ids": pending,
        "warnings": warnings,
        "form_reference": "דוח תקופתי למע\"מ — מספר הטופס/מבנה השידור טרם אומת ולכן אינו מקודד",
    }


def build_income_tax_advance(s: Session, biz: Business, start: date, end: date) -> dict:
    docs = _docs(s, biz.id, start, end, by_period=True)
    income = [d for d in docs if d.status == "confirmed" and d.direction == "income"]
    turnover = 0.0
    withholding = 0.0
    for d in income:
        sign = -1 if d.extracted.get("doc_type") == "credit_invoice" else 1
        turnover += sign * d.decision["pre_vat_amount"]
        withholding += d.extracted.get("withholding_tax", 0.0) or 0.0
    gross = turnover * biz.advance_rate_percent / 100
    warnings = []
    if biz.advance_rate_percent == 0:
        warnings.append("שיעור המקדמות של העסק הוא 0% — יש להזין את השיעור שקבע פקיד השומה")
    pending = [d.id for d in docs if d.status == "pending_review" and d.direction == "income"]
    if pending:
        warnings.append(f"{len(pending)} מסמכי הכנסה ממתינים לבדיקה ולא נכללו")
    net = gross - withholding
    if net < 0:
        warnings.append("הניכוי במקור עולה על המקדמה — היתרה אינה מועברת אוטומטית; לבדיקת המייצג")
    return {
        "kind": "income_tax_advance",
        "business": {"name": biz.name, "vat_id": biz.vat_id},
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "turnover_pre_vat": r2(turnover),
        "advance_rate_percent": biz.advance_rate_percent,
        "gross_advance": r2(gross),
        "withholding_credit": r2(withholding),
        "advance_payable": r2(max(0.0, net)),
        "included_document_ids": [d.id for d in income],
        "pending_document_ids": pending,
        "warnings": warnings,
        "form_reference": "מקדמות מס הכנסה — מספר הטופס טרם אומת ולכן אינו מקודד",
    }


def income_tax(taxable: float, year: int, credit_points: float) -> tuple[float, list[dict], data.TaxYear]:
    ty = data.tax_year(year)
    tax, lower, rows = 0.0, 0.0, []
    for ceiling, rate in ty.brackets:
        if taxable <= lower:
            break
        part = min(taxable, ceiling) - lower
        tax += part * rate
        rows.append({"from": lower, "to": None if ceiling == float("inf") else ceiling, "rate": rate, "tax": r2(part * rate)})
        lower = ceiling
    surtax = max(0.0, taxable - ty.surtax_threshold) * ty.surtax_rate
    credits = credit_points * ty.credit_point_annual
    total = max(0.0, tax + surtax - credits)
    rows.append({"surtax": r2(surtax), "credits": r2(credits)})
    return total, rows, ty


def _asset_cost_sign(extracted: dict) -> int:
    """זיכוי מספק על רכוש קבוע מקטין את עלות הנכס ואת הפחת."""
    return -1 if extracted.get("doc_type") == "credit_invoice" else 1


def _fixed_asset_docs(s: Session, business_id: int, upto_year: int) -> list[Document]:
    """רכוש קבוע שנרכש בכל שנה עד `upto_year` — נדרש כי הפחת נמשך על פני שנים."""
    return [d for d in s.scalars(select(Document).where(
        Document.business_id == business_id, Document.direction == "expense", Document.status == "confirmed",
        Document.doc_date.is_not(None), Document.doc_date <= date(upto_year, 12, 31)).order_by(Document.doc_date))
        if d.decision.get("is_fixed_asset")]


def build_annual_estimate(s: Session, biz: Business, year: int, credit_points: float,
                          donations: float = 0.0, carried_loss: float = 0.0) -> dict:
    from .. import depreciation as dep
    from ..tax_model import estimate_burden
    start, end = date(year, 1, 1), date(year, 12, 31)
    docs = _docs(s, biz.id, start, end)
    confirmed = [d for d in docs if d.status == "confirmed"]
    revenue = 0.0
    by_cat: dict[str, float] = defaultdict(float)
    for d in confirmed:
        if d.direction == "income":
            sign = -1 if d.extracted.get("doc_type") == "credit_invoice" else 1
            revenue += sign * d.decision["pre_vat_amount"]
        elif not d.decision.get("is_fixed_asset"):
            by_cat[d.decision["category"]] += d.decision["recognized_expense"]

    depreciation_lines = []
    for d in _fixed_asset_docs(s, biz.id, year):
        dec = d.decision
        cost = (dec.get("pre_vat_amount") or 0.0) + ((dec.get("vat_amount") or 0.0) - (dec.get("deductible_vat") or 0.0))
        cls = dec.get("asset_class") or "other"
        rate = data.DEPRECIATION[cls].value
        sign = _asset_cost_sign(d.extracted)
        amount = sign * dep.depreciation_for_year(cost, d.doc_date, rate, year)
        cost *= sign
        if amount != 0:
            depreciation_lines.append({"document_id": d.id, "vendor": d.extracted.get("vendor_name"),
                                       "purchased": d.doc_date.isoformat(), "asset_class": cls, "rate": rate,
                                       "cost": r2(cost), "depreciation": amount})
    depreciation_total = sum(x["depreciation"] for x in depreciation_lines)
    if depreciation_total:
        by_cat["depreciation"] += depreciation_total

    expenses = sum(by_cat.values())
    profit = revenue - expenses
    burden = estimate_burden(profit, year, credit_points, carried_loss=carried_loss, donations=donations)

    warnings = ["הערכה בלבד — אינה דוח שנתי. לא כולל ניכויי פנסיה/קרן השתלמות (ראו היועץ), הכנסות אחרות, והתאמות אחרות"]
    if not burden["tax_table_verified"]:
        warnings.append(f"טבלת המס לשנת {year} לא אומתה: {burden['tax_table_note']}")
    if not burden["social_security"]["verified"]:
        warnings.append("שיעורי הביטוח הלאומי, התקרה והניכוי של 52% לא אומתו מול טבלת המוסד")
    if depreciation_lines:
        warnings.append(f"פחת על {len(depreciation_lines)} נכסים לפי שיעורים שלא אומתו (קו ישר, יחסי לימים)")
    if carried_loss:
        warnings.append("הפסד מועבר: נדרשת בדיקה שהוא מדווח ומאושר בשנה שבה נוצר")
    if burden["social_security"]["minimum_applied"]:
        warnings.append("הכנסה מתחת למזערית: דמי ביטוח לאומי חושבו על ההכנסה המזערית")
    pending = _pending(docs)
    if pending:
        warnings.append(f"{len(pending)} מסמכים ממתינים לבדיקה ולא נכללו")

    # מס ששולם מראש: ניכוי במקור שנוכה על ידי לקוחות + מקדמות בדוחות מאושרים (שכבר קיזזו את הניכוי של תקופתם)
    withholding = sum((d.extracted.get("withholding_tax") or 0.0) for d in confirmed if d.direction == "income")
    advance_reports = list(s.scalars(select(Report).where(
        Report.business_id == biz.id, Report.kind == "income_tax_advance", Report.status == "approved",
        Report.period_start >= start, Report.period_start <= end)))
    advances = sum(r.payload.get("advance_payable", 0.0) for r in advance_reports)
    prepaid = r2(withholding + advances)
    balance = r2(burden["income_tax"] - prepaid)
    warnings.append("מקדמות נלקחו רק מדוחות מקדמה שאושרו במערכת; מקדמות ששולמו מחוץ למערכת או קנסות/ריבית לא נכללו")
    if balance < 0:
        warnings.append(f"לפי ההערכה שולם מראש יותר מהמס — צפוי החזר של כ-{-balance:,.0f} ₪ אחרי הגשת הדוח השנתי. "
                        "אפשר לשקול בקשה להפחתת שיעור המקדמות")

    return {
        "kind": "annual_estimate",
        "business": {"name": biz.name, "vat_id": biz.vat_id, "owner": biz.owner_name},
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "report_inputs": {"credit_points": credit_points, "donations": donations, "carried_loss": carried_loss},
        "revenue_pre_vat": r2(revenue),
        "recognized_expenses_by_category": {k: r2(v) for k, v in sorted(by_cat.items())},
        "recognized_expenses_total": r2(expenses),
        "business_profit": r2(profit),
        "depreciation": depreciation_lines,
        "taxable_income": burden["taxable_income"],
        "credit_points": credit_points,
        "donation_credit": burden["donation_credit"],
        "estimated_income_tax": burden["income_tax"],
        "social_security": burden["social_security"],
        "total_burden": burden["total_burden"],
        "effective_rate": burden["effective_rate"],
        "tax_breakdown": burden["tax_breakdown"],
        "tax_table_source": {"verified": burden["tax_table_verified"], "note": burden["tax_table_note"]},
        "prepaid": {"withholding": r2(withholding), "advances": r2(advances), "total": prepaid,
                    "advance_report_ids": [r.id for r in advance_reports]},
        "income_tax_balance": balance,
        "included_document_ids": [d.id for d in confirmed],
        "pending_document_ids": pending,
        "warnings": warnings,
        "form_reference": "הדוח השנתי ליחיד (1301) מוגש על ידי הנישום/מייצג — כאן הכנה ותמיכה בלבד",
    }
