"""נתוני רגולציה מגורסים לפי תאריך.

כל ערך כאן נושא מקור משפטי ודגל `verified`. ערך שלא אומת מול מקור רשמי
מכריח בדיקה אנושית בכל מסמך שהוא נוגע בו. שינוי חקיקה = שינוי נתונים כאן,
לא שינוי קוד.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Source:
    citation: str
    verified: bool
    note: str = ""


# --- שיעור מע"מ ---------------------------------------------------------
# (תאריך תחילה, שיעור). השיעור החל הוא האחרון שתאריך תחילתו <= תאריך המסמך.
VAT_RATES: list[tuple[date, float]] = [
    (date(2013, 6, 2), 0.18),
    (date(2015, 10, 1), 0.17),
    (date(2025, 1, 1), 0.18),
]
VAT_RATE_SOURCE = Source(
    'חוק מס ערך מוסף, התשל"ו-1975, סעיף 2; צו מס ערך מוסף (שיעור המס על עסקאות ועל יבוא טובין)',
    verified=True,
    note="18% מ-1.1.2025",
)

# --- חשבוניות ישראל: סף חובת מספר הקצאה (סכום לפני מע"מ) -----------------
# None = אין חובה. החובה חלה כאשר הסכום לפני מע"מ עולה על הסף.
ALLOCATION_THRESHOLDS: list[tuple[date, float | None]] = [
    (date(1900, 1, 1), None),
    (date(2024, 5, 5), 25_000.0),
    (date(2025, 1, 1), 20_000.0),
    (date(2026, 1, 1), 10_000.0),
    (date(2026, 6, 1), 5_000.0),
]
ALLOCATION_SOURCE = Source(
    'חוק מס ערך מוסף, סעיף 38 כנוסחו במודל "חשבוניות ישראל" (חוק ההתייעלות הכלכלית 2023-2024) '
    "והאצת לוח הזמנים שאושרה בוועדת הכספים",
    verified=True,
    note="סף 5,000 ₪ מ-1.6.2026. מקורות משניים חלוקים אם 'מעל' או 'ומעלה' — מיושם 'מעל', ויש לוודא",
)

# --- מסמכים שמתירים ניכוי מס תשומות ----------------------------------------
VAT_DEDUCTIBLE_DOC_TYPES = {"tax_invoice", "tax_invoice_receipt"}
DOC_TYPE_SOURCE = Source(
    'חוק מס ערך מוסף, סעיף 38(א); תקנות מע"מ, תקנה 18 — ניכוי תשומות רק על פי חשבונית מס',
    verified=True,
)


# --- סיווג הוצאות ----------------------------------------------------------
@dataclass(frozen=True)
class CategoryRule:
    key: str
    label_he: str
    income_tax_recognition: float  # 0..1 מתוך הסכום לפני מע"מ (או כולל, אם אין ניכוי מע"מ)
    vat_deduction: float  # 0..1 מתוך מס התשומות
    source: Source
    keywords: tuple[str, ...] = ()
    is_fixed_asset: bool = False


CATEGORIES: dict[str, CategoryRule] = {
    r.key: r
    for r in [
        CategoryRule(
            "office_supplies", "ציוד משרדי מתכלה", 1.0, 1.0,
            Source("פקודת מס הכנסה, סעיף 17; חוק מע\"מ, סעיף 38", verified=True),
            ("נייר", "טונר", "דיו", "ציוד משרדי", "אופיס דיפו", "office depot", "כלי כתיבה", "מדפסת"),
        ),
        CategoryRule(
            "software", "תוכנה ומנויים", 1.0, 1.0,
            Source("פקודת מס הכנסה, סעיף 17; חוק מע\"מ, סעיף 38", verified=True),
            ("תוכנה", "מנוי", "רישיון", "microsoft", "google", "adobe", "aws", "github", "zoom", "dropbox", "saas"),
        ),
        CategoryRule(
            "inventory", "קניית סחורה / מלאי", 1.0, 1.0,
            Source("פקודת מס הכנסה, סעיפים 17 ו-85; חוק מע\"מ, סעיף 38", verified=True),
            ("סחורה", "מלאי", "חומרי גלם"),
        ),
        CategoryRule(
            "professional_services", "שירותים מקצועיים", 1.0, 1.0,
            Source("פקודת מס הכנסה, סעיף 17; חוק מע\"מ, סעיף 38", verified=True),
            ("ייעוץ", "עורך דין", "עו\"ד", "הנהלת חשבונות", "שירותי", "פרסום", "שיווק"),
        ),
        CategoryRule(
            "vehicle", "רכב — אחזקה ודלק", 0.45, 2 / 3,
            Source(
                'תקנות מס הכנסה (ניכוי הוצאות רכב), התשנ"ה-1995; תקנות מע"מ, תקנה 18 '
                "(2/3 ברכב בשימוש עיקרי לעסק, 1/4 בשימוש מעורב)",
                verified=False,
                note="שיעור ההכרה תלוי בפרופיל השימוש ובשווי השימוש — חובה לאמת לפי מצב העסק",
            ),
            ("דלק", "סונול", "פז ", "דור אלון", "ten ", "תדלוק", "מוסך", "צמיגים", "חניה", "חניון",
             "כביש 6", "ליסינג", "טסט", "ביטוח רכב"),
        ),
        CategoryRule(
            "refreshments", "כיבוד קל במקום העסק", 0.80, 0.0,
            Source(
                'תקנות מס הכנסה (ניכוי הוצאות מסוימות), התשל"ב-1972 — 80% מכיבוד קל; '
                "תקנות מע\"מ, תקנה 18 — הגבלת ניכוי על אירוח",
                verified=False,
                note="שיעור ניכוי המע\"מ על כיבוד קל במקום העסק טעון אימות",
            ),
            ("כיבוד", "קפה", "מים מינרליים", "עוגיות", "שתייה"),
        ),
        CategoryRule(
            "hospitality", "אירוח / ארוחות עסקיות", 0.0, 0.0,
            Source(
                'תקנות מס הכנסה (ניכוי הוצאות מסוימות), התשל"ב-1972 — אירוח אינו מותר (למעט אורח מחו"ל); '
                "תקנות מע\"מ, תקנה 18",
                verified=False,
                note="חריג אירוח אורחים מחו\"ל לא ממומש — נבדק ידנית",
            ),
            ("מסעדה", "ארוחה", "בית קפה", "מסעדת", "אירוח"),
        ),
        CategoryRule(
            "equipment", "ציוד / רכוש קבוע", 0.0, 1.0,
            Source(
                'תקנות מס הכנסה (פחת), 1941 — מוכר בפחת ולא כהוצאה שוטפת; חוק מע"מ, סעיף 38 (תשומות ציוד)',
                verified=True,
                note="ההוצאה נרשמת ללוח פחת; ההכרה השוטפת כאן 0%",
            ),
            ("מחשב נייד", "לפטופ", "laptop", "מקרן", "ריהוט", "שולחן", "כיסא משרדי"),
            is_fixed_asset=True,
        ),
        CategoryRule(
            "unknown", "לא מסווג", 0.0, 0.0,
            Source("לא נמצא כלל מתאים — נדרש סיווג אנושי", verified=False),
        ),
    ]
}


# --- מס הכנסה ליחיד (הערכה בלבד) ------------------------------------------
@dataclass(frozen=True)
class TaxYear:
    brackets: list[tuple[float, float]]  # (תקרה שנתית, שיעור); תקרה אחרונה = inf
    surtax_threshold: float
    surtax_rate: float
    credit_point_annual: float
    source: Source


_BRACKETS_2025 = [
    (84_120, 0.10), (120_720, 0.14), (193_800, 0.20), (269_280, 0.31),
    (560_280, 0.35), (float("inf"), 0.47),
]

TAX_YEARS: dict[int, TaxYear] = {
    2025: TaxYear(
        _BRACKETS_2025, 721_560, 0.03, 2_904,
        Source('פקודת מס הכנסה, סעיף 121 ו-121ב (מס יסף); מדרגות מוקפאות לשנת 2025', verified=True),
    ),
    2026: TaxYear(
        _BRACKETS_2025, 721_560, 0.03, 2_904,
        Source(
            "מדרגות 2025 המוקפאות",
            verified=False,
            note="דווח על הרחבה רטרואקטיבית של מדרגות 20% ו-31% לשנת 2026 — יש לעדכן לאחר אימות מול רשות המסים",
        ),
    ),
}


# --- תכנון מס לגיטימי (מודול היועץ) -------------------------------------------
# כל פרמטר כאן הוא נתון "כפי שדווח במקורות משניים" אלא אם verified=True.
# מקורות משניים נסתרו זה בזה בכמה נקודות — ראו docs/research-log-2026-10.md.
PLANNING_SOURCE_NOTE = "נתון מדווח ממקורות משניים (אתרי פנסיה/רואי חשבון), לא מנוסח חוק רשמי"


@dataclass(frozen=True)
class Param:
    value: float
    source: Source


PENSION_SELF_EMPLOYED = {
    "ceiling_income": Param(232_800, Source(
        "פקודת מס הכנסה, סעיפים 45א ו-47 — הכנסה מזכה לעצמאי", False, PLANNING_SOURCE_NOTE)),
    "deduction_rate": Param(0.11, Source("פקודת מס הכנסה, סעיף 47 — ניכוי עד 11%", False, PLANNING_SOURCE_NOTE)),
    "deduction_cap": Param(25_608, Source("סעיף 47 — תקרת ניכוי 2026 (11% מ-232,800)", False, PLANNING_SOURCE_NOTE)),
    "credit_rate": Param(0.055, Source("פקודת מס הכנסה, סעיף 45א — הפקדה מזכה עד 5.5%", False, PLANNING_SOURCE_NOTE)),
    "credit_cap": Param(12_804, Source("סעיף 45א — תקרה (5.5% מ-232,800)", False, PLANNING_SOURCE_NOTE)),
    "credit_percent": Param(0.35, Source("סעיף 45א — זיכוי של 35% מההפקדה המזכה", False, PLANNING_SOURCE_NOTE)),
}

KEREN_HISHTALMUT_SELF_EMPLOYED = {
    "ceiling_income": Param(293_397, Source("הכנסה קובעת לעצמאי לצורך קרן השתלמות 2026", False, PLANNING_SOURCE_NOTE)),
    "deduction_rate": Param(0.045, Source("קרן השתלמות לעצמאי — ניכוי עד 4.5%", False, PLANNING_SOURCE_NOTE)),
    # מקור אחד מדווח 13,203; מקור אחר מדווח 20,566 כתקרת הפקדה מוטבת — סותרים, מיושם הנמוך
    "deduction_cap": Param(13_203, Source(
        "קרן השתלמות לעצמאי — תקרת ניכוי 2026", False,
        PLANNING_SOURCE_NOTE + ". מקור אחר מדווח 20,566 — סתירה, נדרש אימות")),
}

CORPORATE = {
    "corp_tax_rate": Param(0.23, Source("פקודת מס הכנסה, סעיף 126 — מס חברות", False, PLANNING_SOURCE_NOTE)),
    "dividend_rate_individual": Param(0.25, Source("סעיף 125 — מס דיבידנד ליחיד", False, PLANNING_SOURCE_NOTE)),
    "dividend_rate_material_holder": Param(0.30, Source("סעיף 125 — מס דיבידנד לבעל מניות מהותי (10%+)", False, PLANNING_SOURCE_NOTE)),
}

VEHICLE_RULE = {
    "recognition_rate": Param(0.45, Source(
        'תקנות מס הכנסה (ניכוי הוצאות רכב), התשנ"ה-1995 — 45%', False, PLANNING_SOURCE_NOTE)),
}

ADVANCE_REDUCTION_FORM = Source(
    "טופס 2216א' — בקשה לביטול או הקטנת מקדמות (שירות gov.il)",
    verified=True,
    note="הטופס קיים; מועד ההגשה והתנאים (לפי מקור משני: עד 31 בינואר) — לאימות",
)
