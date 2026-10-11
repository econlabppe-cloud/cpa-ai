"""בדיקות דטרמיניסטיות בודדות. כל פונקציה מחזירה ממצאים עם מקור משפטי."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import data


@dataclass
class Finding:
    code: str
    severity: str  # "error" | "warning" | "info"
    message: str
    source: str = ""

    def as_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "message": self.message, "source": self.source}


@dataclass
class RuleTrace:
    findings: list[Finding] = field(default_factory=list)
    applied: list[dict] = field(default_factory=list)

    def add(self, *f: Finding) -> None:
        self.findings.extend(f)

    def apply(self, rule: str, value, source: data.Source) -> None:
        self.applied.append(
            {"rule": rule, "value": value, "source": source.citation, "verified": source.verified, "note": source.note}
        )


def _effective(table: list[tuple[date, object]], d: date):
    value = None
    for start, v in table:
        if start <= d:
            value = v
    return value


def vat_rate_for(d: date) -> float:
    rate = _effective(data.VAT_RATES, d)
    if rate is None:
        raise ValueError(f"אין שיעור מע\"מ מוגדר לתאריך {d}")
    return rate


def allocation_threshold_for(d: date) -> float | None:
    return _effective(data.ALLOCATION_THRESHOLDS, d)


def validate_israeli_id(raw: str | None) -> bool:
    """ספרת ביקורת למספר ת.ז / עוסק מורשה / ח.פ (9 ספרות, משקלות 1,2 לסירוגין)."""
    if not raw:
        return False
    digits = raw.replace("-", "").replace(" ", "")
    if not digits.isdigit() or len(digits) > 9:
        return False
    digits = digits.zfill(9)
    if digits == "000000000":
        return False
    total = 0
    for i, ch in enumerate(digits):
        n = int(ch) * (1 if i % 2 == 0 else 2)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def check_amounts(pre_vat: float | None, vat: float | None, total: float | None, rate: float,
                  doc_type: str) -> tuple[float | None, float | None, float | None, list[Finding]]:
    """משלים סכומים חסרים ובודק עקביות. מחזיר (pre, vat, total, findings)."""
    f: list[Finding] = []
    src = data.VAT_RATE_SOURCE.citation
    known = sum(x is not None for x in (pre_vat, vat, total))

    if total is None and pre_vat is not None and vat is not None:
        total = round(pre_vat + vat, 2)
    elif pre_vat is None and total is not None and vat is not None:
        pre_vat = round(total - vat, 2)
    elif vat is None and total is not None and pre_vat is not None:
        vat = round(total - pre_vat, 2)
    elif known == 1 and total is not None and doc_type in data.VAT_DEDUCTIBLE_DOC_TYPES:
        pre_vat = round(total / (1 + rate), 2)
        vat = round(total - pre_vat, 2)
        f.append(Finding("AMOUNTS_DERIVED", "warning",
                         f"רק סכום כולל זוהה; הסכום לפני מע\"מ והמע\"מ חושבו לפי {rate:.0%}", src))
    elif known == 1 and total is not None:
        pre_vat, vat = total, 0.0

    if total is None:
        f.append(Finding("TOTAL_MISSING", "error", "סכום כולל לא זוהה"))
        return pre_vat, vat, total, f

    if pre_vat is not None and vat is not None and abs(pre_vat + vat - total) > 1.0:
        f.append(Finding("SUM_MISMATCH", "error",
                         f"לפני מע\"מ ({pre_vat:,.2f}) + מע\"מ ({vat:,.2f}) ≠ סה\"כ ({total:,.2f})"))

    if doc_type in data.VAT_DEDUCTIBLE_DOC_TYPES and pre_vat is not None and vat is not None:
        expected = pre_vat * rate
        if vat == 0:
            f.append(Finding("VAT_ZERO_ON_TAX_INVOICE", "warning",
                             "חשבונית מס ללא מע\"מ — ייתכן עסקה פטורה/בשיעור אפס; יש לוודא", src))
        elif abs(vat - expected) > max(1.0, expected * 0.005):
            f.append(Finding("VAT_RATE_MISMATCH", "error",
                             f"המע\"מ בחשבונית ({vat:,.2f}) אינו תואם {rate:.0%} מהסכום ({expected:,.2f})", src))
    return pre_vat, vat, total, f


def check_allocation(doc_date: date, doc_type: str, pre_vat: float | None,
                     allocation_number: str | None) -> list[Finding]:
    if doc_type not in data.VAT_DEDUCTIBLE_DOC_TYPES or pre_vat is None:
        return []
    threshold = allocation_threshold_for(doc_date)
    if threshold is None or pre_vat <= threshold:
        return []
    src = data.ALLOCATION_SOURCE.citation
    if not allocation_number:
        return [Finding("ALLOCATION_MISSING", "error",
                        f"חשבונית מעל {threshold:,.0f} ₪ לפני מע\"מ ללא מספר הקצאה — אסור לנכות את מס התשומות", src)]
    digits = "".join(ch for ch in allocation_number if ch.isdigit())
    if len(digits) < 6:
        return [Finding("ALLOCATION_MALFORMED", "error", f"מספר הקצאה '{allocation_number}' אינו תקין במבנהו", src)]
    return [Finding("ALLOCATION_PRESENT", "info",
                    "מספר הקצאה קיים — יש לאמת מול מערכת חשבוניות ישראל לפני הגשת הדוח", src)]
