from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import Business, Document
from ..reports import periodic
from . import planner


def advise(s: Session, biz: Business, year: int, credit_points: float, existing_pension: float,
           existing_keren: float, material_holder: bool) -> dict:
    est = periodic.build_annual_estimate(s, biz, year, credit_points)
    docs = list(s.scalars(select(Document).where(
        Document.business_id == biz.id, Document.status == "confirmed",
        Document.doc_date >= date(year, 1, 1), Document.doc_date <= date(year, 12, 31))))
    expense_docs = [
        {"id": d.id, "extracted": d.extracted, "decision": d.decision}
        for d in docs if d.direction == "expense"
    ]
    # מסמכים שנחסמו בחוסר מספר הקצאה לא נכנסו לאישור — לכן נבדקים גם הממתינים
    pending = list(s.scalars(select(Document).where(
        Document.business_id == biz.id, Document.status == "pending_review",
        Document.direction == "expense", Document.doc_date >= date(year, 1, 1),
        Document.doc_date <= date(year, 12, 31))))
    expense_docs += [{"id": d.id, "extracted": d.extracted, "decision": d.decision} for d in pending]

    plan = planner.build_plan(
        year=year,
        profit=est["business_profit"],
        turnover=est["revenue_pre_vat"],
        advance_rate_pct=biz.advance_rate_percent,
        estimated_tax=est["estimated_income_tax"],
        credit_points=credit_points,
        existing_pension=existing_pension,
        existing_keren=existing_keren,
        material_holder=material_holder,
        vat_blocked=planner.blocked_vat(expense_docs),
        vehicle=planner.vehicle_review(expense_docs),
    )
    plan["inputs"] = {
        "profit": est["business_profit"], "revenue_pre_vat": est["revenue_pre_vat"],
        "estimated_income_tax": est["estimated_income_tax"], "credit_points": credit_points,
        "advance_rate_percent": biz.advance_rate_percent,
        "existing_pension": existing_pension, "existing_keren": existing_keren,
        "material_holder": material_holder,
    }
    return plan
