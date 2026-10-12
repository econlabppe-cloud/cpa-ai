"""מודל נטל המס של עצמאי: מס הכנסה + ביטוח לאומי ובריאות, עם הפסד מועבר, תרומות וניכויים.

משמש גם את ההערכה השנתית וגם את מחשבון ה-what-if, כך שמספרים זהים בשניהם.
כל הפרמטרים מגיעים מ-rules/data.py עם מקור ודגל אימות; תוצאה שתלויה בנתון לא מאומת מסומנת.
"""
from __future__ import annotations

from .reports.periodic import income_tax
from .rules import data


def social_security(annual_income: float) -> dict:
    """דמי ביטוח לאומי ובריאות לעצמאי. הכנסה מתחת למזערית משלמת על המזערית (גם הכנסה שלילית)."""
    p = data.SOCIAL_SECURITY
    monthly = max(annual_income, 0.0) / 12
    base = min(max(monthly, p["min_income_monthly"].value), p["ceiling_monthly"].value)
    low_part = min(base, p["low_threshold_monthly"].value)
    high_part = max(base - p["low_threshold_monthly"].value, 0.0)
    monthly_total = low_part * p["low_rate"].value + high_part * p["high_rate"].value
    total = monthly_total * 12
    return {
        "total": round(total, 2),
        "deductible_from_income": round(total * p["deductible_share"].value, 2),
        "monthly_base": round(base, 2),
        "minimum_applied": monthly < p["min_income_monthly"].value,
        "capped": monthly > p["ceiling_monthly"].value,
        "verified": all(v.source.verified for v in p.values()),
    }


def donation_credit(donations: float, taxable_income: float) -> float:
    d = data.DONATIONS
    if donations < d["min_total"].value:
        return 0.0
    eligible = min(donations, taxable_income * d["income_share_cap"].value, d["absolute_cap"].value)
    return round(max(eligible, 0.0) * d["credit_rate"].value, 2)


def estimate_burden(profit: float, year: int, credit_points: float, *, carried_loss: float = 0.0,
                    donations: float = 0.0, extra_deduction: float = 0.0, extra_credit: float = 0.0) -> dict:
    """profit: רווח עסקי לפני הפסד מועבר וניכויים. extra_deduction: למשל הפקדות לפנסיה/קרן (ניכוי).
    extra_credit: זיכויים נוספים במס (למשל זיכוי פנסיה לפי סעיף 45א)."""
    ss = social_security(profit)
    taxable = max(0.0, profit - max(carried_loss, 0.0) - ss["deductible_from_income"] - max(extra_deduction, 0.0))
    tax_after_points, rows, ty = income_tax(taxable, year, credit_points)
    don = donation_credit(donations, taxable)
    tax = max(0.0, tax_after_points - don - max(extra_credit, 0.0))
    return {
        "profit": round(profit, 2),
        "carried_loss_used": round(min(max(carried_loss, 0.0), max(profit - ss["deductible_from_income"], 0.0)), 2),
        "taxable_income": round(taxable, 2),
        "income_tax": round(tax, 2),
        "income_tax_before_donations": round(tax_after_points, 2),
        "donation_credit": don,
        "social_security": ss,
        "total_burden": round(tax + ss["total"], 2),
        "effective_rate": round((tax + ss["total"]) / profit, 4) if profit > 0 else 0.0,
        "tax_breakdown": rows,
        "tax_table_verified": ty.source.verified,
        "tax_table_note": ty.source.note,
    }
