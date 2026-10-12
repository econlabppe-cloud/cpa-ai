"""אסטרטגיות ובדיקות נוספות ליועץ. כל אחת מחזירה מבנה זהה לשאר האסטרטגיות, או None אם אינה רלוונטית."""
from __future__ import annotations

from datetime import date

from ..rules import data

ENTITLEMENTS = [
    "אישה (נקודה נוספת)", "הורה לילדים (לפי גיל הילד)", "חייל/ת משוחרר/ת", "בוגר/ת תואר אקדמי או לימודים מוכרים",
    "עולה חדש או תושב חוזר", "תושב ישוב מזכה", "בן/בת זוג שאינו מרוויח", "הורה יחיד", "נכות או עיוורון",
]


def _basis(*sources: data.Source) -> list[dict]:
    return [{"citation": s.citation, "verified": s.verified, "note": s.note} for s in sources]


def _s(sid, title, details, actions, risk, basis, saving=0.0, status="needs_verification") -> dict:
    return {"id": sid, "title": title, "status": status, "estimated_annual_saving": saving, "details": details,
            "actions": actions, "risk": risk, "basis": basis}


def year_end_calendar(year: int, today: date) -> dict | None:
    if today.year != year:
        return None
    end = date(year, 12, 31)
    nxt = date(year + 1, 1, 31)
    items = [
        {"date": end.isoformat(), "days_left": (end - today).days,
         "action": "להשלים הפקדות לפנסיה ולקרן השתלמות — הפקדה שבוצעה אחרי סוף השנה נספרת לשנה הבאה"},
        {"date": end.isoformat(), "days_left": (end - today).days,
         "action": "תרומות למוסדות עם אישור לפי סעיף 46 — להעביר עד סוף השנה ולקבל קבלה על שם התורם"},
        {"date": end.isoformat(), "days_left": (end - today).days,
         "action": "ציוד שמתוכנן לעסק: מע\"מ התשומות מוחזר מיד, אבל הפחת השנה יחסי לימים שנותרו — ראו מחשבון מה-אם"},
        {"date": nxt.isoformat(), "days_left": (nxt - today).days,
         "action": "בקשה להקטנת מקדמות (טופס 2216א') אם המקדמות גבוהות מהמס הצפוי — מועד אחרון לפי מקור משני: 31 בינואר"},
    ]
    return _s("year_end_calendar", "לוח פעולות לסוף השנה",
              {"days_to_year_end": (end - today).days, "items": items},
              [f"עד סוף השנה נותרו {(end - today).days} ימים — הסעיפים למטה תלויי תאריך"],
              "מועדים וכללי התזמון לא אומתו מול רשות המסים; לוודא עם המייצג",
              _basis(data.ADVANCE_REDUCTION_FORM, data.PENSION_SELF_EMPLOYED["deduction_rate"].source))


def credit_points_review(credit_points: float) -> dict | None:
    if credit_points > 2.25:
        return None  # כבר הוזנו נקודות מעבר לבסיס של תושב ישראל
    value = data.tax_year(2026).credit_point_annual
    return _s("credit_points_review", "בדיקת נקודות זיכוי",
              {"value_per_point": value, "base_points_assumed": credit_points, "possible_entitlements": ENTITLEMENTS},
              [f"כל נקודת זיכוי נוספת שווה עד ₪{value:,.0f} בשנה — לבדוק זכאות ברשימה",
               "לעדכן את מספר הנקודות בטופס 101 / בבקשה לפקיד השומה, ולהזין אותו כאן"],
              "זכאות נקבעת לפי נסיבות אישיות ומסמכים; לא ניתן לנחש",
              _basis(data.TAX_YEARS[2025].source))


def status_review(turnover: float, input_vat_ytd: float, marginal: float) -> dict | None:
    ceiling = data.EXEMPT_DEALER_CEILING.value
    if turnover > ceiling:
        return None
    cost = round(input_vat_ytd * (1 - marginal), 2)
    return _s("status_review", "עוסק מורשה מול עוסק פטור",
              {"turnover": round(turnover, 2), "ceiling": ceiling, "below_ceiling": True,
               "input_vat_recovered_ytd": round(input_vat_ytd, 2), "net_cost_if_exempt_after_tax": cost},
              [f"המחזור ({turnover:,.0f} ₪) מתחת לתקרת הפטור ({ceiling:,.0f} ₪)",
               f"כעוסק פטור מע\"מ התשומות ({input_vat_ytd:,.0f} ₪ השנה) לא היה מוחזר: עלות נטו ≈ ₪{cost:,.0f} אחרי מס",
               "פטור מתאים בעיקר ללקוחות פרטיים ולהוצאות קטנות; מורשה עדיף ללקוחות עסקיים ולהוצאות ציוד גבוהות"],
              "מעבר סטטוס כרוך בדיווח למע\"מ, בתנאי זכאות (חלק מהמקצועות חייבים להיות מורשים) ובשינוי מחירים ללקוחות",
              _basis(data.EXEMPT_DEALER_CEILING.source))


def cash_discipline(cash_docs: list[dict]) -> dict | None:
    limit = data.CASH_LIMIT.value
    near = [d for d in cash_docs if d["total"] >= limit * 0.5]
    if not near:
        return None
    over = [d for d in near if d["total"] >= limit]
    lo, hi = data.CASH_PENALTY_RANGE
    return _s("cash_discipline", "תשלומים במזומן",
              {"documents": len(near), "over_limit": len(over), "limit": limit, "items": near[:10],
               "penalty_range": [lo, hi]},
              ["לשלם בהעברה בנקאית, אשראי או שיק — כך ההוצאה מתועדת וגם נמנעת חשיפה לקנס",
               "לא לפצל עסקה כדי לעקוף את התקרה — פיצול נחשב הפרה",
               f"קנס מדווח {lo:.0%}–{hi:.0%} מסכום העסקה, וייתכן שההוצאה לא תוכר"],
              "הסף (6,000 מול 11,000 ₪) וההשלכה על הכרה בהוצאה לא אומתו",
              _basis(data.CASH_LIMIT.source))


def foreign_invoices(foreign_docs: list[dict]) -> dict | None:
    if not foreign_docs:
        return None
    return _s("foreign_invoices", "הוצאות מספקים זרים",
              {"documents": len(foreign_docs), "items": foreign_docs[:10]},
              ["להוריד חשבונית מחשבון הפלטפורמה (Google, Meta וכד') — דף הוצאה בלבד לא מספיק כמסמך",
               "לרשום את הסכום בש\"ח לפי החיוב בפועל בכרטיס האשראי",
               "לשאול את המייצג אם נדרשת חשבונית עצמית לדיווח מע\"מ על ייבוא שירותים (מקורות סותרים)"],
              "הוצאה ללא מסמך תקף עלולה לא להיות מוכרת",
              _basis(data.FOREIGN_VAT_SOURCE))


def loss_carryforward(profit: float) -> dict | None:
    if profit >= 0:
        return None
    return _s("loss_carryforward", "הפסד עסקי השנה",
              {"loss": round(-profit, 2)},
              ["הפסד עסקי ניתן להעברה וקיזוז כנגד הכנסה עסקית בשנים הבאות — לוודא שהוא מדווח בדוח השנתי",
               "בדוח השנה הבאה להזין אותו בשדה 'הפסד מועבר'"],
              "תנאי הדיווח והאישור של ההפסד לא אומתו; ביטוח לאומי משולם גם בשנת הפסד (על ההכנסה המזערית)",
              _basis(data.LOSS_CARRYFORWARD_SOURCE))
