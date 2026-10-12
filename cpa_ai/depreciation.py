"""לוח פחת לרכוש קבוע (קו ישר, יחסי לימים בשנת הרכישה). כל השיעורים לא מאומתים — ראו rules/data.py."""
from __future__ import annotations

from datetime import date

from .rules import data


def classify_asset(vendor: str | None, line_items: list[str]) -> str:
    hay = " ".join([vendor or "", *line_items]).lower()
    for cls in ("computer", "furniture", "office_equipment"):
        if any(k.lower() in hay for k in data.DEPRECIATION_KEYWORDS[cls]):
            return cls
    return "other"


def depreciation_for_year(cost: float, purchased: date, rate: float, year: int) -> float:
    """הפחת שמוכר בשנת `year` לנכס שנרכש ב-`purchased`. השנה הראשונה יחסית לימים שנותרו (כולל יום הרכישה)."""
    if year < purchased.year or cost <= 0 or rate <= 0:
        return 0.0
    full = cost * rate
    first_days = (date(purchased.year, 12, 31) - purchased).days + 1
    first_year_amount = full * first_days / 365
    if year == purchased.year:
        return round(min(first_year_amount, cost), 2)
    already = first_year_amount + full * (year - purchased.year - 1)
    return round(max(0.0, min(full, cost - already)), 2)


def schedule(cost: float, purchased: date, rate: float, years: int = 10) -> list[dict]:
    return [{"year": y, "amount": depreciation_for_year(cost, purchased, rate, y)}
            for y in range(purchased.year, purchased.year + years)
            if depreciation_for_year(cost, purchased, rate, y) > 0]
