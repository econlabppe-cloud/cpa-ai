"""אופטימיזציית מס משולבת לעצמאי: איך לחלק תקציב פנוי בין המנופים כך שהנטל הכולל יהיה מינימלי.

שלא כמו המלצה לכל מנוף בנפרד, כאן כל שקל נבדק במודל הנטל המלא (מס הכנסה + ביטוח לאומי, מדרגות, יסף,
נקודות זיכוי): ניכוי מוריד את השיעור השולי, ולכן הערך של כל שקל נוסף משתנה. האלגוריתם חמדני בצעדים —
בכל צעד השקל הבא הולך למנוף שחוסך הכי הרבה עכשיו. מנופים: קרן השתלמות (ניכוי), פנסיה בערוץ הניכוי
(סעיף 47) ובערוץ הזיכוי (סעיף 45א), ותרומות שהלקוח ממילא מתכנן (לעולם לא ממליצים לתרום בשביל המס).

כל הפרמטרים לא מאומתים (rules/data.py) — התוצאה להכנה לשיחה עם המייצג, לא החלטה.
"""
from __future__ import annotations

from ..rules import data
from ..tax_model import estimate_burden
from . import planner

STEP = 500.0

LEVERS = {
    "keren_hishtalmut": {"label": "קרן השתלמות", "liquidity": "נזיל אחרי 6 שנים; רווחים פטורים ממס (בכפוף לתקרה)"},
    "pension_deduction": {"label": "פנסיה — ערוץ ניכוי (סעיף 47)", "liquidity": "נעול עד פרישה; מגדיל קצבה"},
    "pension_credit": {"label": "פנסיה — ערוץ זיכוי 35% (סעיף 45א)", "liquidity": "נעול עד פרישה; מגדיל קצבה"},
    "donation": {"label": "תרומה מתוכננת למוסד מוכר (סעיף 46)", "liquidity": "הכסף יוצא — רק תרומה שממילא מתכננים"},
}


def _caps(profit: float, year: int, existing_pension: float, existing_keren: float,
          planned_donations: float, donations_done: float) -> dict[str, float]:
    base_marginal = planner.marginal_rate(max(profit, 0.0), year)
    pen = planner.pension_plan(profit, base_marginal, existing_pension)
    ker = planner.keren_hishtalmut_plan(profit, base_marginal, existing_keren)
    return {"keren_hishtalmut": ker["room"], "pension_deduction": pen["room_deduction"],
            "pension_credit": pen["room_credit"], "donation": max(planned_donations - donations_done, 0.0)}


def _burden(profit, year, cp, carried_loss, donations_done, amounts: dict[str, float]) -> float:
    credit_pct = data.PENSION_SELF_EMPLOYED["credit_percent"].value
    return estimate_burden(
        profit, year, cp, carried_loss=carried_loss,
        donations=donations_done + amounts.get("donation", 0.0),
        extra_deduction=amounts.get("keren_hishtalmut", 0.0) + amounts.get("pension_deduction", 0.0),
        extra_credit=amounts.get("pension_credit", 0.0) * credit_pct)["total_burden"]


def optimize(profit: float, year: int, credit_points: float, budget: float, *, existing_pension: float = 0.0,
             existing_keren: float = 0.0, carried_loss: float = 0.0, planned_donations: float = 0.0,
             donations_done: float = 0.0, step: float = STEP) -> dict:
    caps = _caps(profit, year, existing_pension, existing_keren, planned_donations, donations_done)
    amounts = {k: 0.0 for k in caps}
    before = _burden(profit, year, credit_points, carried_loss, donations_done, amounts)
    current, left = before, max(budget, 0.0)
    steps = []
    while left > 0.5:
        best = None
        for lever, cap in caps.items():
            room = cap - amounts[lever]
            if room <= 0.5:
                continue
            add = min(step, room, left)
            trial = {**amounts, lever: amounts[lever] + add}
            b = _burden(profit, year, credit_points, carried_loss, donations_done, trial)
            gain = (current - b) / add
            if best is None or gain > best[0] + 1e-9:
                best = (gain, lever, add, b)
        if best is None or best[0] <= 0:
            break
        gain, lever, add, b = best
        amounts[lever] += add
        left -= add
        current = b
        steps.append((lever, gain))

    allocation = []
    for lever in caps:
        amt = round(amounts[lever], 2)
        if amt <= 0:
            continue
        alone = before - _burden(profit, year, credit_points, carried_loss, donations_done, {lever: amt})
        rates = [g for lv, g in steps if lv == lever]
        allocation.append({"lever": lever, "label": LEVERS[lever]["label"], "amount": amt,
                           "cap_remaining": round(caps[lever] - amt, 2),
                           "saving_if_alone": round(alone, 2),
                           "saving_per_shekel": round(sum(rates) / len(rates), 4) if rates else 0.0,
                           "net_cost": round(amt - alone, 2) if lever == "donation" else None,
                           "liquidity": LEVERS[lever]["liquidity"]})
    first: dict[str, int] = {}
    for i, (lv, _) in enumerate(steps):
        first.setdefault(lv, i)
    allocation.sort(key=lambda a: first.get(a["lever"], len(steps)))
    return {
        "budget": round(budget, 2),
        "allocation": allocation,
        "unused_budget": round(left, 2),
        "burden_before": round(before, 2),
        "burden_after": round(current, 2),
        "total_saving": round(before - current, 2),
        "caps": {k: round(v, 2) for k, v in caps.items()},
        "note": ("החיסכון מחושב במודל הנטל המלא אחרי כל ההפקדות יחד (לא סכום של חיסכונות נפרדים). "
                 "הפקדה לפנסיה ולקרן אינה הוצאה — הכסף נשאר שלכם, נעול לתקופה"),
        "verified": False,
    }


def bracket_position(taxable: float, year: int) -> dict:
    """איפה ההכנסה החייבת יושבת במדרגות: כמה ניכוי יוריד מדרגה וכמה הכנסה נוספת תעלה מדרגה."""
    ty = data.tax_year(year)
    lower = 0.0
    for ceiling, rate in ty.brackets:
        if taxable <= ceiling:
            return {"taxable": round(taxable, 2), "marginal_rate": planner.marginal_rate(taxable, year),
                    "bracket_rate": rate, "bracket_from": lower,
                    "bracket_to": None if ceiling == float("inf") else ceiling,
                    "to_lower_bracket": round(taxable - lower, 2) if lower > 0 else None,
                    "to_next_bracket": None if ceiling == float("inf") else round(ceiling - taxable, 2),
                    "surtax_applies": taxable > ty.surtax_threshold, "table_verified": ty.source.verified}
        lower = ceiling
    raise AssertionError("unreachable")


def income_timing(this_year_profit: float, next_year_profit: float, year: int, credit_points: float,
                  step: float = 5_000.0) -> dict:
    """האם יש ערך בהזזת הכנסה לשנה הבאה (כשהשנה הבאה צפויה נמוכה). מחזיר את ההזזה הטובה ביותר."""
    def total(shift):
        return (estimate_burden(this_year_profit - shift, year, credit_points)["total_burden"]
                + estimate_burden(next_year_profit + shift, year + 1, credit_points)["total_burden"])
    base = total(0.0)
    best_shift, best = 0.0, base
    s = step
    while s <= max(this_year_profit, 0.0) / 2:
        t = total(s)
        if t < best - 0.5:
            best_shift, best = s, t
        s += step
    return {
        "best_shift": best_shift, "saving": round(base - best, 2),
        "this_year_marginal": planner.marginal_rate(max(this_year_profit, 0), year),
        "next_year_marginal": planner.marginal_rate(max(next_year_profit, 0), year + 1),
        "legal_note": ("רק תזמון אמיתי: עבודה שתבוצע/תסופק בשנה הבאה, או מועד גבייה כשהעסק מדווח על בסיס מזומן. "
                       "חשבונית מס חייבת לצאת במועד העסקה לפי חוק מע\"מ — דחיית חשבונית על עבודה שכבר סופקה אסורה. "
                       "בסיס הדיווח (מזומן/מצטבר) של העסק — לבדיקת המייצג"),
        "verified": False,
    }


def reserve_duty(planned_days: int, ni_monthly_base: float, actual_monthly_income: float) -> dict:
    """תגמול מילואים לעצמאי מחושב לפי המקדמות לביטוח לאומי ב-3 החודשים שלפני השירות. אם ההכנסה האמיתית
    גבוהה מבסיס המקדמות (למשל בסיס ישן משנה חלשה) — עדכון הבסיס לפני השירות מגדיל את התגמול."""
    r = data.RESERVE_DUTY
    lo, hi = r["min_daily"].value, r["max_daily"].value

    def daily(monthly):
        return round(min(max(monthly * 3 / r["base_days"].value, lo), hi), 2)

    now = daily(ni_monthly_base)
    updated = daily(max(actual_monthly_income, ni_monthly_base))
    extra = round(max(updated - now, 0.0) * max(planned_days, 0), 2)
    return {
        "planned_days": planned_days, "daily_now": now, "daily_if_updated": updated,
        "extra_compensation": extra,
        "condition": ("רק כשההכנסה החודשית האמיתית גבוהה מבסיס המקדמות. עדכון לבסיס גבוה מההכנסה האמיתית הוא "
                      "דיווח כוזב, וייתכן שהתגמול יתואם לפי השומה הסופית"),
        "actions": ["לבקש מביטוח לאומי לעדכן את הכנסת המקדמות לפי ההכנסה הצפויה — לפני תחילת השירות",
                    "לשמור את אישור העדכון ואת דוחות ההכנסה שמבססים אותו",
                    "לבדוק זכאות לנקודות זיכוי ממס בשל שירות מילואים (סעיף 39ב) ולמענקים ייעודיים לעצמאים"],
        "cash_effect": "המקדמות הגבוהות משולמות מוקדם יותר, אבל אם ההכנסה אמיתית — הן ממילא חלק מהחיוב השנתי",
        "basis": [vars(p.source) for p in r.values()][:1],
        "verified": all(p.source.verified for p in r.values()),
    }
