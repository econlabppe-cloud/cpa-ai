"""מנוע הכללים: מקבל עובדות שחולצו, מחזיר החלטת מס דטרמיניסטית עם עקבה משפטית."""
from __future__ import annotations

import calendar
from datetime import date

from .. import depreciation
from ..schemas import ExtractedDocument, TaxDecision
from . import checks, data
from .checks import Finding, RuleTrace

REVIEW_CONFIDENCE_THRESHOLD = 0.85


def classify(extracted: ExtractedDocument, override: str | None = None) -> tuple[data.CategoryRule, Finding | None]:
    if override:
        if override not in data.CATEGORIES:
            raise ValueError(f"קטגוריה לא מוכרת: {override}")
        return data.CATEGORIES[override], Finding("CATEGORY_MANUAL", "info", "קטגוריה נקבעה ידנית על ידי בודק")

    haystack = " ".join([extracted.vendor_name or "", *extracted.line_items]).lower() + " "
    scores: dict[str, int] = {}
    for rule in data.CATEGORIES.values():
        hits = sum(1 for kw in rule.keywords if kw.lower() in haystack)
        if hits:
            scores[rule.key] = hits
    if scores:
        best = max(scores, key=scores.get)
        tied = [k for k, v in scores.items() if v == scores[best]]
        if len(tied) > 1:
            return data.CATEGORIES["unknown"], Finding(
                "CATEGORY_AMBIGUOUS", "warning", f"התאמה לכמה קטגוריות: {', '.join(tied)}")
        hint = extracted.category_hint
        if hint and hint in data.CATEGORIES and hint != best:
            return data.CATEGORIES[best], Finding(
                "CATEGORY_HINT_CONFLICT", "warning", f"הצעת המודל '{hint}' שונה מהכלל '{best}'")
        return data.CATEGORIES[best], None

    hint = extracted.category_hint
    if hint and hint in data.CATEGORIES and hint != "unknown":
        return data.CATEGORIES[hint], Finding(
            "CATEGORY_FROM_HINT", "warning", "קטגוריה לפי הצעת המודל בלבד — ללא התאמת מילות מפתח")
    return data.CATEGORIES["unknown"], Finding("CATEGORY_UNKNOWN", "warning", "לא נמצאה קטגוריה מתאימה")


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def evaluate_expense(extracted: ExtractedDocument, category_override: str | None = None,
                     report_date: date | None = None) -> TaxDecision:
    """report_date: תחילת התקופה שבה המסמך ידווח, כשהיא מאוחרת מתקופת המסמך (מסמך שהגיע באיחור)."""
    trace = RuleTrace()

    if extracted.doc_date is None:
        trace.add(Finding("DATE_MISSING", "error", "תאריך המסמך לא זוהה — לא ניתן לקבוע שיעור מע\"מ"))
        rate = None
    else:
        rate = checks.vat_rate_for(extracted.doc_date)
        trace.apply("vat_rate", rate, data.VAT_RATE_SOURCE)

    foreign = extracted.is_foreign_vendor
    # חשבונית זיכוי מספק מבטלת חלק מחשבונית מס קודמת: סכומיה נבדקים כמו חשבונית מס, ותוצאתה שלילית
    credit = extracted.doc_type == "credit_invoice"
    vat_doc = credit or extracted.doc_type in data.VAT_DEDUCTIBLE_DOC_TYPES
    pre, vat, total = extracted.pre_vat_amount, extracted.vat_amount, extracted.total_amount
    if foreign:
        # חשבונית זרה: בלי מע"מ ישראלי, ואין מספר עוסק ישראלי. הסכום נקבע בש"ח לפי שער ביום החיוב
        total = total if total is not None else pre
        pre, vat = total, 0.0
        if total is None:
            trace.add(Finding("TOTAL_MISSING", "error", "סכום כולל לא זוהה"))
        if extracted.currency != "ILS":
            trace.add(Finding("FOREIGN_CURRENCY", "warning",
                              f"החשבונית ב-{extracted.currency}. יש להזין את הסכום בש\"ח לפי השער ביום החיוב "
                              "(איזה שער נדרש לא אומת) ולשנות את המטבע ל-ILS לפני אישור"))
        trace.add(Finding("FOREIGN_VAT_NOTE", "warning",
                          "ספק זר: אין ניכוי מע\"מ תשומות. ייתכן שנדרשת חשבונית עצמית לדיווח מע\"מ (ייבוא שירותים) — "
                          "מקורות סותרים, לבדיקת המייצג", data.FOREIGN_VAT_SOURCE.citation))
    elif rate is not None:
        pre, vat, total, f = checks.check_amounts(pre, vat, total, rate, "tax_invoice" if credit else extracted.doc_type)
        trace.add(*f)
    elif total is None:
        trace.add(Finding("TOTAL_MISSING", "error", "סכום כולל לא זוהה"))

    if extracted.doc_type == "unknown":
        trace.add(Finding("DOC_TYPE_UNKNOWN", "error", "סוג המסמך לא זוהה"))
    elif credit:
        trace.add(Finding("CREDIT_NOTE", "warning",
                          "חשבונית זיכוי מספק: מקטינה את ההוצאה ואת מע\"מ התשומות שנוכה. יש לוודא שהחשבונית "
                          "המקורית נרשמה ושהזיכוי לא נרשם כבר", data.DOC_TYPE_SOURCE.citation))
    elif extracted.doc_type not in data.VAT_DEDUCTIBLE_DOC_TYPES:
        trace.add(Finding("NO_VAT_DEDUCTION_DOC", "info",
                          "המסמך אינו חשבונית מס — לא ניתן לנכות ממנו מע\"מ תשומות", data.DOC_TYPE_SOURCE.citation))

    vendor_valid = checks.validate_israeli_id(extracted.vendor_id)
    if foreign:
        vendor_valid = False  # אין מספר עוסק ישראלי; לא שגיאה
    elif not extracted.vendor_id:
        sev = "error" if vat_doc else "warning"
        trace.add(Finding("VENDOR_ID_MISSING", sev, "מספר עוסק של הספק לא זוהה"))
    elif not vendor_valid:
        trace.add(Finding("VENDOR_ID_INVALID", "error", f"ספרת הביקורת של מספר העוסק {extracted.vendor_id} שגויה"))

    if extracted.doc_date is not None and not foreign:
        trace.add(*checks.check_allocation(extracted.doc_date, extracted.doc_type, pre, extracted.allocation_number))

    category, cat_finding = classify(extracted, category_override)
    if cat_finding:
        trace.add(cat_finding)
    trace.apply("category", category.key, category.source)
    if not category.source.verified:
        trace.add(Finding("RULE_UNVERIFIED", "warning",
                          f"כלל הסיווג '{category.label_he}' לא אומת מול מקור רשמי — {category.source.note or 'נדרש אישור מקצועי'}",
                          category.source.citation))

    blocked_vat = any(f.code in {"ALLOCATION_MISSING", "ALLOCATION_MALFORMED", "VENDOR_ID_INVALID"} for f in trace.findings)
    vat_ok = extracted.doc_type in data.VAT_DEDUCTIBLE_DOC_TYPES and not blocked_vat and not foreign
    window = int(data.INPUT_VAT_WINDOW_MONTHS.value)
    if (vat_ok and not credit and report_date and extracted.doc_date
            and report_date > add_months(extracted.doc_date, window)):
        vat_ok = False
        trace.add(Finding("VAT_DEDUCTION_LATE", "warning",
                          f"החשבונית מ-{extracted.doc_date:%d/%m/%Y} מדווחת בתקופה שמתחילה ב-{report_date:%d/%m/%Y} — "
                          f"מעבר ל-{window} חודשים, ולכן מע\"מ התשומות אינו מנוכה (נכלל בעלות ההוצאה). לבדיקת המייצג",
                          data.INPUT_VAT_WINDOW_MONTHS.source.citation))
    if credit:
        # ביטול ניכוי הוא שמרני: מקטינים את התשומות גם כשמספר העוסק שגוי
        vat_ok = not foreign

    cash_over = (extracted.payment_method == "cash" and total is not None and total >= data.CASH_LIMIT.value)
    if cash_over:
        lo, hi = data.CASH_PENALTY_RANGE
        trace.add(Finding("CASH_LIMIT", "warning",
                          f"תשלום במזומן של {total:,.0f} ₪ מעל התקרה בין עסקים ({data.CASH_LIMIT.value:,.0f} ₪). "
                          f"קנס מדווח {lo:.0%}–{hi:.0%} וייתכן שההוצאה לא תוכר. הסף לא אומת (מקורות סותרים) — לבדיקת המייצג",
                          data.CASH_LIMIT.source.citation))
    vat_rate_ded = category.vat_deduction if vat_ok else 0.0
    deductible_vat = round((vat or 0.0) * vat_rate_ded, 2)
    # מע"מ שאינו מנוכה הופך לחלק מעלות ההוצאה
    expense_base = (pre or 0.0) + ((vat or 0.0) - deductible_vat)
    recognized = round(expense_base * category.income_tax_recognition, 2)
    if credit:
        deductible_vat, recognized = -deductible_vat, -recognized
    if category.is_fixed_asset:
        trace.add(Finding("FIXED_ASSET", "info", "רכוש קבוע — יש להוסיף ללוח הפחת", category.source.citation))

    errors = sum(f.severity == "error" for f in trace.findings)
    warnings = sum(f.severity == "warning" for f in trace.findings)
    confidence = extracted.extraction_confidence * (0.6 ** errors) * (0.92 ** warnings)
    needs_review = (
        confidence < REVIEW_CONFIDENCE_THRESHOLD
        or errors > 0
        or not category.source.verified
        or category.key == "unknown"
        or cash_over
        or credit
        or any(f.code == "FOREIGN_CURRENCY" for f in trace.findings)
    )

    return TaxDecision(
        category=category.key,
        category_label=category.label_he,
        vat_rate=rate,
        pre_vat_amount=pre,
        vat_amount=vat,
        total_amount=total,
        vendor_id_valid=vendor_valid,
        currency=extracted.currency,
        payment_method=extracted.payment_method,
        is_foreign_vendor=foreign,
        income_tax_recognition=category.income_tax_recognition,
        recognized_expense=recognized,
        vat_deduction_rate=round(vat_rate_ded, 4),
        deductible_vat=deductible_vat,
        is_fixed_asset=category.is_fixed_asset,
        asset_class=depreciation.classify_asset(extracted.vendor_name, extracted.line_items) if category.is_fixed_asset else None,
        confidence=round(confidence, 3),
        needs_human_review=needs_review,
        findings=[f.as_dict() for f in trace.findings],
        applied_rules=trace.applied,
    )


def evaluate_income(doc_date, doc_type: str, pre_vat: float, vat: float | None,
                    customer_id: str | None, allocation_number: str | None) -> dict:
    """עסקה (הכנסה): מחשב מע"מ עסקאות ובודק חובת מספר הקצאה לחשבונית שהופקה."""
    rate = checks.vat_rate_for(doc_date)
    findings: list[Finding] = []
    expected = round(pre_vat * rate, 2)
    if vat is None:
        vat = expected
    elif abs(vat - expected) > max(1.0, expected * 0.005):
        findings.append(Finding("VAT_RATE_MISMATCH", "error", f"מע\"מ {vat:,.2f} אינו תואם {rate:.0%} ({expected:,.2f})",
                                data.VAT_RATE_SOURCE.citation))
    if customer_id and not checks.validate_israeli_id(customer_id):
        findings.append(Finding("CUSTOMER_ID_INVALID", "error", f"מספר העוסק של הלקוח {customer_id} שגוי"))
    alloc = checks.check_allocation(doc_date, doc_type, pre_vat, allocation_number)
    # בהכנסה, היעדר מספר הקצאה פוגע בלקוח — אצלנו זו אזהרה לפני הפקה
    findings.extend(Finding(f.code, "warning" if f.severity == "error" else f.severity, f.message, f.source) for f in alloc)
    return {
        "vat_rate": rate,
        "pre_vat_amount": round(pre_vat, 2),
        "vat_amount": round(vat, 2),
        "total_amount": round(pre_vat + vat, 2),
        "findings": [f.as_dict() for f in findings],
        "needs_human_review": any(f.severity == "error" for f in findings),
    }
