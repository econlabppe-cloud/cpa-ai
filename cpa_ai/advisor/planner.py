"""יועץ תכנון מס — אופטימיזציה לגיטימית בתוך החוק.

עקרונות:
- כל המלצה נושאת בסיס משפטי (citation) ודגל אימות. המלצה שתלויה בנתון לא מאומת
  מסומנת `needs_verification` והסכום שלה הוא הערכה בלבד.
- המערכת לא מציעה דבר שמסתיר הכנסה, יוצר מסמכים פיקטיביים, או מכניס הוצאות פרטיות
  כעסקיות. ראו NOT_OFFERED.
- הדו"ח הוא הכנה לשיחה עם מייצג מורשה, לא ייעוץ מס מחייב.
"""
from __future__ import annotations

from ..reports.periodic import income_tax
from ..rules import data

NOT_OFFERED = [
    "אי-הוצאת חשבונית או הוצאת מסמך שאינו משקף עסקה אמיתית",
    "הכנסה שלא מדווחת, או דחיית הכנסה לשנה אחרת בלי בסיס עסקי",
    "הכללת הוצאות פרטיות (משפחה, בית, חופשות) כהוצאות עסק",
    "חשבוניות או עסקאות בין קרובים שאין להן טעם מסחרי מעבר להפחתת מס (סעיף 86 לפקודה — עסקה מלאכותית)",
]

ADVISOR_DISCLAIMER = (
    "הדו\"ח הכנה לשיחה עם מייצג מורשה (רו\"ח / יועץ מס / עו\"ד). אינו ייעוץ מס מחייב. "
    "סכומי החיסכון הם הערכה לפי נתונים מדווחים שחלקם טרם אומתו."
)


def _status(verified: bool) -> str:
    return "ready" if verified else "needs_verification"


def _basis(*sources: data.Source) -> list[dict]:
    return [{"citation": s.citation, "verified": s.verified, "note": s.note} for s in sources]


def marginal_rate(taxable: float, year: int) -> float:
    ty = data.TAX_YEARS[year]
    rate = ty.brackets[-1][1]
    for ceiling, r in ty.brackets:
        if taxable <= ceiling:
            rate = r
            break
    if taxable > ty.surtax_threshold:
        rate += ty.surtax_rate
    return rate


def pension_plan(profit: float, marginal: float, existing: float) -> dict:
    """מחלק את ההפקדה הפנויה בין ערוץ הניכוי (סעיף 47) לערוץ הזיכוי (סעיף 45א).

    הערוץ עם הערך הגבוה למס יתמלא קודם: ניכוי שווה marginal לשקל, זיכוי שווה 35%.
    """
    p = data.PENSION_SELF_EMPLOYED
    base = min(max(profit, 0.0), p["ceiling_income"].value)
    ded_cap = min(base * p["deduction_rate"].value, p["deduction_cap"].value)
    cred_cap = min(base * p["credit_rate"].value, p["credit_cap"].value)
    channels = [
        ("deduction", marginal, ded_cap),
        ("credit", p["credit_percent"].value, cred_cap),
    ]
    channels.sort(key=lambda c: c[1], reverse=True)

    remaining_existing = max(existing, 0.0)
    room: dict[str, float] = {}
    for name, _, cap in channels:
        used = min(remaining_existing, cap)
        remaining_existing -= used
        room[name] = cap - used

    saving = room["deduction"] * marginal + room["credit"] * p["credit_percent"].value
    return {
        "base_income": round(base, 2),
        "room_deduction": round(room["deduction"], 2),
        "room_credit": round(room["credit"], 2),
        "estimated_saving": round(saving, 2),
        "better_channel": channels[0][0],
    }


def keren_hishtalmut_plan(profit: float, marginal: float, existing: float) -> dict:
    k = data.KEREN_HISHTALMUT_SELF_EMPLOYED
    base = min(max(profit, 0.0), k["ceiling_income"].value)
    cap = min(base * k["deduction_rate"].value, k["deduction_cap"].value)
    room = max(cap - max(existing, 0.0), 0.0)
    return {"base_income": round(base, 2), "room": round(room, 2), "estimated_saving": round(room * marginal, 2)}


def corporate_comparison(profit: float, year: int, credit_points: float, material_holder: bool) -> dict:
    """השוואת הכנסה מעוסק מורשה מול חברה בע"מ (כל הרווח מחולק כדיבידנד).

    לא כולל: ביטוח לאומי (משמעותי מאוד לעוסק מורשה), מס יסף על הכנסה פסיבית, רווחים צבורים,
    עלויות הקמה ותחזוקה, וזמן. לכן זו השוואה חלקית שמצדיקה בדיקה מקצועית, לא המלצה.
    """
    c = data.CORPORATE
    ind_tax, _, _ = income_tax(max(profit, 0.0), year, credit_points)
    net_individual = profit - ind_tax

    corp_tax = profit * c["corp_tax_rate"].value
    after_corp = profit - corp_tax
    div_rate = c["dividend_rate_material_holder"].value if material_holder else c["dividend_rate_individual"].value
    div_tax = after_corp * div_rate
    net_company_distributed = after_corp - div_tax

    return {
        "profit": round(profit, 2),
        "individual": {"tax": round(ind_tax, 2), "net": round(net_individual, 2),
                       "effective_rate": round(ind_tax / profit, 4) if profit > 0 else 0.0},
        "company_full_distribution": {"corp_tax": round(corp_tax, 2), "dividend_tax": round(div_tax, 2),
                                      "net": round(net_company_distributed, 2),
                                      "effective_rate": round(1 - net_company_distributed / profit, 4) if profit > 0 else 0.0},
        "difference_company_minus_individual": round(net_company_distributed - net_individual, 2),
        "excluded": ["ביטוח לאומי", "מס יסף על הכנסה פסיבית", "רווחים צבורים", "עלויות הקמה ותחזוקה"],
        "basis": _basis(c["corp_tax_rate"].source, c["dividend_rate_individual"].source),
        "status": _status(False),
    }


def advance_reduction(turnover: float, advance_rate_pct: float, estimated_tax: float) -> dict | None:
    """אם המקדמות שמשולמות גבוהות מהמס המשוער — שווה לבקש הקטנה (טופס 2216א')."""
    if advance_rate_pct <= 0:
        return None
    advances = turnover * advance_rate_pct / 100
    excess = advances - estimated_tax
    if excess <= 0:
        return None
    return {
        "annual_advances": round(advances, 2),
        "estimated_tax": round(estimated_tax, 2),
        "estimated_cash_overpaid": round(excess, 2),
        "estimated_saving": 0.0,
        "note": "זה איננו חיסכון במס אלא שחרור נזילות — המס הסופי לא משתנה",
    }


def blocked_vat(docs: list[dict]) -> dict:
    """מע"מ תשומות שנחסם בגלל חסר מספר הקצאה — פעולה ישירה: לבקש מספר הקצאה מהספק."""
    items = []
    total = 0.0
    for d in docs:
        codes = {f["code"] for f in d["decision"]["findings"]}
        if "ALLOCATION_MISSING" in codes:
            vat = d["decision"].get("vat_amount") or 0.0
            total += vat
            items.append({"document_id": d["id"], "vendor": d["extracted"].get("vendor_name"), "vat": round(vat, 2)})
    return {
        "items": items,
        "total_vat_blocked": round(total, 2),
        "action": "לבקש מהספק חשבונית מעודכנת עם מספר הקצאה (אם העסקה תקפה), ולנכות את המע\"מ בתקופה הנוכחית",
        "basis": _basis(data.ALLOCATION_SOURCE),
        "status": _status(False),
    }


def vehicle_review(docs: list[dict]) -> dict:
    """מציג כמה מע"מ נוכה על רכב לפי השיעור הנוכחי — לבדיקה מול הכלל."""
    rows = [d for d in docs if d["decision"]["category"] == "vehicle"]
    deducted = sum(d["decision"]["deductible_vat"] for d in rows)
    return {
        "documents": len(rows),
        "vat_deducted": round(deducted, 2),
        "note": "שיעור ניכוי המע\"מ על רכב (2/3 לפי הכלל במערכת) לא אומת מול מקור רשמי. אם לא, הניכוי חייב להיות מתוקן",
        "status": _status(False),
    }


def build_plan(
    *,
    year: int,
    profit: float,
    turnover: float,
    advance_rate_pct: float,
    estimated_tax: float,
    credit_points: float,
    existing_pension: float,
    existing_keren: float,
    material_holder: bool,
    vat_blocked: dict,
    vehicle: dict,
) -> dict:
    # הניכוי מקטין את ההכנסה החייבת, ולכן המדרגה השולית נמדדת אחריו. מחשבים פעמיים לכל היותר.
    marginal = marginal_rate(max(profit, 0.0), year)
    for _ in range(2):
        pension = pension_plan(profit, marginal, existing_pension)
        keren = keren_hishtalmut_plan(profit, marginal, existing_keren)
        taxable_after = max(profit - pension["room_deduction"] - keren["room"], 0.0)
        new_marginal = marginal_rate(taxable_after, year)
        if new_marginal == marginal:
            break
        marginal = new_marginal
    pension = pension_plan(profit, marginal, existing_pension)
    keren = keren_hishtalmut_plan(profit, marginal, existing_keren)
    corp = corporate_comparison(profit, year, credit_points, material_holder)
    adv = advance_reduction(turnover, advance_rate_pct, estimated_tax)

    strategies = [
        {
            "id": "pension_self_employed",
            "title": "הפקדה לפנסיה לעצמאי (ניכוי + זיכוי)",
            "status": _status(False),
            "estimated_annual_saving": pension["estimated_saving"],
            "details": pension,
            "actions": [
                f"הפקדה נוספת בערוץ {'ניכוי' if pension['better_channel'] == 'deduction' else 'זיכוי'} "
                "תעדיף את הערוץ בעל הערך הגבוה",
                "לבדוק מה כבר הופקד השנה בקופות ובביטוחים (הזנה כקלט)",
                "לוודא שמדובר בהכנסה מזכה ובהפקדה לקופה מוכרת",
            ],
            "risk": "נזילות — הכסף נעול עד הגיל הקבוע בתקנות; ההכנסה הכוללת לניכוי נבדקת מול הכנסה מזכה",
            "basis": _basis(*(data.PENSION_SELF_EMPLOYED[k].source for k in ("deduction_rate", "credit_rate"))),
        },
        {
            "id": "keren_hishtalmut",
            "title": "קרן השתלמות לעצמאי",
            "status": _status(False),
            "estimated_annual_saving": keren["estimated_saving"],
            "details": keren,
            "actions": ["לבדוק תקרת ניכוי מול רשות המסים לפני הפקדה — המקורות סותרים (13,203 מול 20,566)"],
            "risk": "נתון תקרה סותר; הכסף נעול לפי תקנות הקרן",
            "basis": _basis(data.KEREN_HISHTALMUT_SELF_EMPLOYED["deduction_cap"].source),
        },
        {
            "id": "vat_blocked_allocation",
            "title": "מע\"מ שנחסם בחוסר מספר הקצאה",
            "status": _status(False),
            "estimated_annual_saving": vat_blocked["total_vat_blocked"],
            "details": vat_blocked,
            "actions": [vat_blocked["action"]],
            "risk": "אם הספק לא מנפיק מספר הקצאה — אין ניכוי; לא לנסות לעקוף",
            "basis": vat_blocked["basis"],
        },
        {
            "id": "vehicle_rule_check",
            "title": "בדיקת כלל הרכב (מע\"מ והכרה)",
            "status": _status(False),
            "estimated_annual_saving": 0.0,
            "details": vehicle,
            "actions": ["לאמת את שיעורי ההכרה והמע\"מ לרכב מול התקנות לפני שנת המס"],
            "risk": "ניכוי רכב שגוי נבדק בביקורת; לשמור יומן נסועה",
            "basis": _basis(data.VEHICLE_RULE["recognition_rate"].source),
        },
        {
            "id": "corporate_vs_individual",
            "title": "השוואה: עוסק מורשה מול חברה בע\"מ",
            "status": "needs_verification",
            "estimated_annual_saving": corp["difference_company_minus_individual"],
            "details": corp,
            "actions": ["לא להחליט על בסיס ההשוואה הזו בלבד — לכלול ביטוח לאומי ועלויות הקמה ותחזוקה"],
            "risk": "החלטה מבנית ארוכת טווח; מעבר לחברה כרוך בעלויות ובהשלכות משפטיות",
            "basis": corp["basis"],
        },
    ]
    if adv:
        strategies.append({
            "id": "advance_reduction",
            "title": "בקשה להקטנת מקדמות (טופס 2216א')",
            "status": _status(False),
            "estimated_annual_saving": 0.0,
            "details": adv,
            "actions": ["להגיש בקשה לפקיד השומה עם הערכת מס מעודכנת ומסמכים שמגבים אותה"],
            "risk": "הגשה שגויה או הערכה שנמוכה מדי עלולה לגרור ריבית ותוספת; ההערכה חייבת להיות ריאלית",
            "basis": _basis(data.ADVANCE_REDUCTION_FORM),
        })

    total = round(sum(s["estimated_annual_saving"] for s in strategies if s["id"] != "corporate_vs_individual"), 2)
    return {
        "year": year,
        "marginal_rate": round(marginal, 4),
        "strategies": strategies,
        "total_estimated_saving_unverified_inputs": total,
        "not_offered": NOT_OFFERED,
        "disclaimer": ADVISOR_DISCLAIMER,
    }
