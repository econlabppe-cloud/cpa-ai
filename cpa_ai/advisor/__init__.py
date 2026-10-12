from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import Business, Document
from ..reports import periodic
from . import planner


def profit_basis(est_profit: float, year: int, today: date, projected: float | None) -> dict:
    """הרווח שעליו מתבסס התכנון. באמצע השנה הרווח עד היום נמוך מהרווח השנתי, ולכן ההמלצות חייבות תחזית."""
    if projected is not None:
        return {"basis": "projected", "profit": projected, "ytd_profit": est_profit, "annualized_suggestion": None,
                "warning": None}
    day_of_year = (today - date(year, 1, 1)).days + 1 if today.year == year else 365
    annualized = round(est_profit * 365 / day_of_year, 2) if 0 < day_of_year < 365 and est_profit > 0 else None
    warning = None
    if today.year == year and day_of_year < 365:
        warning = ("הרווח מבוסס על נתונים עד היום בלבד, ולכן ההמלצות עלולות לזלזל בפוטנציאל. להזין תחזית רווח שנתי "
                   "(projected_profit)" + (f"; הערכה לפי קצב עד כה: ₪{annualized:,.0f}" if annualized else ""))
    return {"basis": "ytd", "profit": est_profit, "ytd_profit": est_profit, "annualized_suggestion": annualized,
            "warning": warning}


def collect(s: Session, biz: Business, year: int) -> dict:
    """מסמכי הוצאה של השנה (מאושרים וממתינים) ומדדים שנגזרים מהם."""
    lo, hi = date(year, 1, 1), date(year, 12, 31)
    docs = list(s.scalars(select(Document).where(
        Document.business_id == biz.id, Document.status.in_(("confirmed", "pending_review")),
        Document.direction == "expense", Document.doc_date >= lo, Document.doc_date <= hi)))
    rows = [{"id": d.id, "extracted": d.extracted, "decision": d.decision, "status": d.status} for d in docs]
    confirmed = [r for r in rows if r["status"] == "confirmed"]
    return {
        "rows": rows,
        "input_vat_ytd": round(sum(r["decision"].get("deductible_vat") or 0.0 for r in confirmed), 2),
        "cash": [{"document_id": r["id"], "vendor": r["extracted"].get("vendor_name"),
                  "total": r["decision"].get("total_amount") or 0.0}
                 for r in rows if r["decision"].get("payment_method") == "cash"],
        "foreign": [{"document_id": r["id"], "vendor": r["extracted"].get("vendor_name"),
                     "currency": r["decision"].get("currency")}
                    for r in rows if r["decision"].get("is_foreign_vendor")],
    }


def advise(s: Session, biz: Business, year: int, credit_points: float, existing_pension: float,
           existing_keren: float, material_holder: bool, *, projected_profit: float | None = None,
           carried_loss: float = 0.0, donations: float = 0.0, today: date | None = None) -> dict:
    today = today or date.today()
    est = periodic.build_annual_estimate(s, biz, year, credit_points, donations, carried_loss)
    pb = profit_basis(est["business_profit"], year, today, projected_profit)
    c = collect(s, biz, year)

    plan = planner.build_plan(
        year=year,
        profit=pb["profit"],
        turnover=est["revenue_pre_vat"],
        advance_rate_pct=biz.advance_rate_percent,
        estimated_tax=est["estimated_income_tax"],
        credit_points=credit_points,
        existing_pension=existing_pension,
        existing_keren=existing_keren,
        material_holder=material_holder,
        vat_blocked=planner.blocked_vat(c["rows"]),
        vehicle=planner.vehicle_review(c["rows"]),
        today=today,
        cash_docs=c["cash"],
        foreign_docs=c["foreign"],
        input_vat_ytd=c["input_vat_ytd"],
    )
    plan["profit_basis"] = pb
    plan["inputs"] = {
        "profit": pb["profit"], "revenue_pre_vat": est["revenue_pre_vat"],
        "estimated_income_tax": est["estimated_income_tax"], "credit_points": credit_points,
        "advance_rate_percent": biz.advance_rate_percent,
        "existing_pension": existing_pension, "existing_keren": existing_keren,
        "material_holder": material_holder, "carried_loss": carried_loss, "donations": donations,
    }
    return plan


def what_if(s: Session, biz: Business, year: int, credit_points: float, existing_pension: float, existing_keren: float,
            carried_loss: float, donations: float, scenarios: list[dict], projected_profit: float | None = None,
            today: date | None = None) -> dict:
    from .whatif import run_what_if
    today = today or date.today()
    est = periodic.build_annual_estimate(s, biz, year, credit_points, donations, carried_loss)
    pb = profit_basis(est["business_profit"], year, today, projected_profit)
    result = run_what_if(profit=pb["profit"], year=year, credit_points=credit_points, carried_loss=carried_loss,
                         donations=donations, existing_pension=existing_pension, existing_keren=existing_keren,
                         scenarios=scenarios)
    result["profit_basis"] = pb
    return result
