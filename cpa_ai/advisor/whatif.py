"""מחשבון "מה אם": כמה ישתנה נטל המס (מס הכנסה + ביטוח לאומי) אם אפעל כך וכך.

דטרמיניסטי ומבוסס על אותו מודל נטל המס של ההערכה השנתית. אין כאן המלצה לעשות משהו, רק כימות של
אפשרות לגיטימית. כל תרחיש מציין אם הנתונים שמאחוריו אומתו, ואילו הערות רלוונטיות (מגבלות, תנאים).
"""
from __future__ import annotations

from datetime import date

from .. import depreciation as dep
from ..rules import checks, data
from ..tax_model import estimate_burden
from . import planner

DISCLAIMER = ("הערכה לצורך שיחה עם מייצג מורשה, לא ייעוץ מס מחייב. תוצאה שתלויה בנתון שלא אומת מסומנת. "
              "הפחתת מס לגיטימית מחייבת עסקה אמיתית עם טעם עסקי; עסקה מלאכותית שמטרתה הפחתת מס עלולה להיפסל (סעיף 86).")

LABELS = {"pension": "הפקדה לפנסיה", "keren": "הפקדה לקרן השתלמות", "equipment": "רכישת ציוד",
          "donation": "תרומה", "home_office": "משרד בבית", "extra_income": "הכנסה נוספת"}


def _basis(*sources: data.Source) -> list[dict]:
    return [{"citation": s.citation, "verified": s.verified, "note": s.note} for s in sources]


def _need(scn: dict, key: str) -> float:
    v = scn.get(key)
    if v is None or float(v) < 0:
        raise ValueError(f"תרחיש '{scn.get('type')}': השדה {key} חסר או שלילי")
    return float(v)


HOME_OFFICE_SOURCE = data.Source(
    "הוצאות משרד בבית — עקרון הפרדה בין שימוש עסקי לפרטי (פקודת מס הכנסה, סעיף 17)", False,
    "מקורות מקצועיים בלבד; אין נוסחה רשמית")


def _pension(scn, ctx) -> dict:
    amount = _need(scn, "amount")
    plan = planner.pension_plan(ctx["profit"], ctx["marginal"], ctx["existing_pension"])
    p = data.PENSION_SELF_EMPLOYED
    credit_first = p["credit_percent"].value > ctx["marginal"]
    rooms = {"credit": plan["room_credit"], "deduction": plan["room_deduction"]}
    order = ["credit", "deduction"] if credit_first else ["deduction", "credit"]
    left, used = amount, {"credit": 0.0, "deduction": 0.0}
    for ch in order:
        take = min(left, rooms[ch])
        used[ch], left = take, left - take
    notes = []
    if left > 0:
        notes.append(f"₪{left:,.0f} מההפקדה מעבר לתקרות ההכרה — לא יניבו הטבת מס (עדיין חיסכון לטווח ארוך)")
    notes.append("הכסף נעול בקופה עד גיל פרישה; לבדוק שהקופה מוכרת ושההפקדה בוצעה עד סוף שנת המס")
    return {"extra_deduction": used["deduction"], "extra_credit": used["credit"] * p["credit_percent"].value,
            "details": {"credit_deposit": round(used["credit"], 2), "deduction_deposit": round(used["deduction"], 2),
                        "not_recognized": round(left, 2), "first_channel": order[0]},
            "notes": notes, "basis": _basis(*(p[k].source for k in ("deduction_rate", "credit_rate", "credit_percent")))}


def _keren(scn, ctx) -> dict:
    amount = _need(scn, "amount")
    room = planner.keren_hishtalmut_plan(ctx["profit"], ctx["marginal"], ctx["existing_keren"])["room"]
    rec = min(amount, room)
    notes = ["תקרת הניכוי סותרת בין מקורות (13,203 מול 20,566 ₪) — יש לאמת לפני הפקדה"]
    if amount > rec:
        notes.append(f"₪{amount - rec:,.0f} מעל תקרת הניכוי — ללא הטבה בשנה זו")
    return {"extra_deduction": rec, "extra_credit": 0.0,
            "details": {"recognized": round(rec, 2), "not_recognized": round(amount - rec, 2)},
            "notes": notes, "basis": _basis(data.KEREN_HISHTALMUT_SELF_EMPLOYED["deduction_cap"].source)}


def _equipment(scn, ctx) -> dict:
    cost = _need(scn, "amount")
    when = date.fromisoformat(scn.get("purchase_date") or f"{ctx['year']}-12-31")
    cls = scn.get("asset_class") or "computer"
    if cls not in data.DEPRECIATION:
        raise ValueError(f"סוג נכס לא מוכר: {cls}")
    rate = data.DEPRECIATION[cls].value
    first = dep.depreciation_for_year(cost, when, rate, when.year)
    threshold = checks.allocation_threshold_for(when)
    alloc = threshold is not None and cost > threshold
    vat = round(cost * checks.vat_rate_for(when), 2)
    notes = []
    if first < cost * rate * 0.25:
        notes.append("פחת השנה קטן כי הוא יחסי לימים שנותרו, אבל מע\"מ התשומות מוחזר מיד בדוח הקרוב")
    if alloc:
        notes.append(f"חשבונית מעל {threshold:,.0f} ₪ לפני מע\"מ: חובה מספר הקצאה, אחרת אין ניכוי מע\"מ ({vat:,.0f} ₪)")
    notes.append("שיעורי הפחת וחישוב השנה הראשונה לא אומתו מול תקנות הפחת")
    return {"profit_delta": -first,
            "details": {"asset_class": cls, "rate": rate, "first_year_depreciation": first,
                        "total_depreciation_all_years": round(sum(r["amount"] for r in dep.schedule(cost, when, rate, 12)), 2),
                        "vat_recovered_cash": vat, "allocation_number_required": alloc},
            "notes": notes, "basis": _basis(data.DEPRECIATION[cls].source)}


def _donation(scn, ctx) -> dict:
    amount = _need(scn, "amount")
    d = data.DONATIONS
    notes = []
    if ctx["donations"] + amount < d["min_total"].value:
        notes.append(f"סך התרומות השנה מתחת לסף הזיכוי ({d['min_total'].value:,.0f} ₪) — אין זיכוי")
    notes.append("הזיכוי רק לתרומה למוסד שיש לו אישור לפי סעיף 46 ועם קבלה על שם התורם")
    return {"donations_delta": amount, "details": {"credit_rate": d["credit_rate"].value}, "notes": notes,
            "basis": _basis(*(d[k].source for k in ("credit_rate", "min_total", "income_share_cap")))}


def _home_office(scn, ctx) -> dict:
    biz, total, costs = _need(scn, "business_rooms"), _need(scn, "total_rooms"), _need(scn, "annual_costs")
    if total <= 0 or biz > total:
        raise ValueError("מספר חדרי העסק חייב להיות חיובי ולא גדול ממספר החדרים הכולל")
    rec = round(costs * biz / total, 2)
    return {"profit_delta": -rec,
            "details": {"recognized_expense": rec, "ratio": round(biz / total, 4)},
            "notes": ["אין כלל רשמי לאחוז: ההפרדה לפי חדרים/שטח היא נוהג בלבד ונבדקת מול הפקיד",
                      "בדירה שכורה — להגדיר בחוזה השכירות שחלק ממנה משמש לעסק, ולקבל אישור ניכוי מס במקור מבעל הדירה",
                      "חדר עסקי שמשמש גם למגורים (למשל חדר ילדים) לא יוכר"],
            "basis": _basis(HOME_OFFICE_SOURCE)}


def _extra_income(scn, ctx) -> dict:
    return {"profit_delta": _need(scn, "amount"), "details": {}, "notes": [], "basis": _basis(*(
        data.SOCIAL_SECURITY[k].source for k in ("low_rate", "high_rate")))}


HANDLERS = {"pension": _pension, "keren": _keren, "equipment": _equipment, "donation": _donation,
            "home_office": _home_office, "extra_income": _extra_income}


def run_what_if(*, profit: float, year: int, credit_points: float, carried_loss: float, donations: float,
                existing_pension: float, existing_keren: float, scenarios: list[dict]) -> dict:
    base = estimate_burden(profit, year, credit_points, carried_loss=carried_loss, donations=donations)
    marginal = planner.marginal_rate(base["taxable_income"], year)
    ctx = {"profit": profit, "year": year, "marginal": marginal, "existing_pension": existing_pension,
           "existing_keren": existing_keren, "donations": donations}

    def burden(mods: dict) -> dict:
        return estimate_burden(profit + mods.get("profit_delta", 0.0), year, credit_points, carried_loss=carried_loss,
                               donations=donations + mods.get("donations_delta", 0.0),
                               extra_deduction=mods.get("extra_deduction", 0.0), extra_credit=mods.get("extra_credit", 0.0))

    def delta(b: dict) -> dict:
        return {"delta_income_tax": round(b["income_tax"] - base["income_tax"], 2),
                "delta_social_security": round(b["social_security"]["total"] - base["social_security"]["total"], 2),
                "delta_total": round(b["total_burden"] - base["total_burden"], 2)}

    out, merged = [], {"profit_delta": 0.0, "extra_deduction": 0.0, "extra_credit": 0.0, "donations_delta": 0.0}
    for scn in scenarios:
        t = scn.get("type")
        if t not in HANDLERS:
            raise ValueError(f"סוג תרחיש לא מוכר: {t}")
        res = HANDLERS[t](scn, ctx)
        mods = {k: res.get(k, 0.0) for k in merged}
        for k in merged:
            merged[k] += mods[k]
        verified = all(b["verified"] for b in res["basis"]) and base["tax_table_verified"]
        out.append({"type": t, "label": LABELS[t], **delta(burden(mods)), "details": res["details"],
                    "notes": res["notes"], "basis": res["basis"], "verified": verified})
    comb = delta(burden(merged))
    return {"year": year, "baseline": {"income_tax": base["income_tax"], "social_security": base["social_security"]["total"],
                                        "total_burden": base["total_burden"], "taxable_income": base["taxable_income"],
                                        "marginal_rate": marginal},
            "scenarios": out, "combined": comb, "disclaimer": DISCLAIMER}
