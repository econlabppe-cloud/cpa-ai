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

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import Business, Document
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


def _docs(s: Session, business_id: int, start: date, end: date) -> list[Document]:
    return list(s.scalars(
        select(Document).where(
            Document.business_id == business_id,
            Document.doc_date >= start,
            Document.doc_date <= end,
            Document.status != "rejected",
        ).order_by(Document.doc_date, Document.id)
    ))


def _pending(docs: list[Document]) -> list[int]:
    return [d.id for d in docs if d.status == "pending_review"]


def _undated_pending(s: Session, business_id: int) -> int:
    return len(list(s.scalars(select(Document.id).where(
        Document.business_id == business_id, Document.doc_date.is_(None), Document.status == "pending_review"))))


def build_vat_period(s: Session, biz: Business, start: date, end: date) -> dict:
    docs = _docs(s, biz.id, start, end)
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
    docs = _docs(s, biz.id, start, end)
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
    if year not in data.TAX_YEARS:
        raise ValueError(f"אין טבלת מס לשנת {year}")
    ty = data.TAX_YEARS[year]
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


def build_annual_estimate(s: Session, biz: Business, year: int, credit_points: float) -> dict:
    start, end = date(year, 1, 1), date(year, 12, 31)
    docs = _docs(s, biz.id, start, end)
    confirmed = [d for d in docs if d.status == "confirmed"]
    revenue = 0.0
    by_cat: dict[str, float] = defaultdict(float)
    fixed_assets = []
    for d in confirmed:
        if d.direction == "income":
            sign = -1 if d.extracted.get("doc_type") == "credit_invoice" else 1
            revenue += sign * d.decision["pre_vat_amount"]
        elif d.decision.get("is_fixed_asset"):
            fixed_assets.append({"document_id": d.id, "vendor": d.extracted.get("vendor_name"),
                                 "date": d.doc_date.isoformat() if d.doc_date else None,
                                 "cost": r2(d.decision["pre_vat_amount"] or 0.0)})
        else:
            by_cat[d.decision["category"]] += d.decision["recognized_expense"]
    expenses = sum(by_cat.values())
    profit = revenue - expenses
    tax, rows, ty = income_tax(max(0.0, profit), year, credit_points)

    warnings = ["הערכה בלבד — אינה דוח שנתי. לא כולל פחת, ניכויי פנסיה/קרן השתלמות, הכנסות אחרות, ביטוח לאומי"]
    if not ty.source.verified:
        warnings.append(f"טבלת המס לשנת {year} לא אומתה: {ty.source.note}")
    if fixed_assets:
        warnings.append(f"{len(fixed_assets)} פריטי רכוש קבוע — יש לחשב פחת בנפרד")
    pending = _pending(docs)
    if pending:
        warnings.append(f"{len(pending)} מסמכים ממתינים לבדיקה ולא נכללו")

    return {
        "kind": "annual_estimate",
        "business": {"name": biz.name, "vat_id": biz.vat_id, "owner": biz.owner_name},
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "revenue_pre_vat": r2(revenue),
        "recognized_expenses_by_category": {k: r2(v) for k, v in sorted(by_cat.items())},
        "recognized_expenses_total": r2(expenses),
        "business_profit": r2(profit),
        "credit_points": credit_points,
        "estimated_income_tax": r2(tax),
        "tax_breakdown": rows,
        "tax_table_source": {"citation": ty.source.citation, "verified": ty.source.verified},
        "fixed_assets": fixed_assets,
        "included_document_ids": [d.id for d in confirmed],
        "pending_document_ids": pending,
        "warnings": warnings,
        "form_reference": "הדוח השנתי ליחיד (1301) מוגש על ידי הנישום/מייצג — כאן הכנה ותמיכה בלבד",
    }
