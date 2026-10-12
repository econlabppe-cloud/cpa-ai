"""מחלץ מבוסס ביטויים רגולריים לטקסט של חשבונית בעברית. פועל ללא רשת וללא מפתחות."""
from __future__ import annotations

import re
from datetime import date

from ..schemas import ExtractedDocument

# מפריד אלפים: פסיק או רווח, ולפחות קבוצה אחת של 3 ספרות; אחרת מספר רגיל. הסדר חשוב:
# חלופה "עד 3 ספרות" כשלעצמה חתכה את 1180.00 ל-118 (ראו tests/test_extraction_numbers.py)
_NUM = r"(-?(?:\d{1,3}(?:[, ]\d{3})+|\d+)(?:\.\d{1,2})?)"
_Q = "[\"'״׳]"  # גרשיים בכל הכתיבים

_ID_RE = re.compile(rf"(?:(?<![במ])ע{_Q}?מ|עוסק\s*מורשה|ח{_Q}?פ|ע\.מ\.?|ח\.פ\.?|(?i:VAT\s*ID))\s*[:.]?\s*(\d[\d\- ]{{6,10}}\d)")
_ALLOC_RE = re.compile(rf"(?:מספר|מס{_Q}?)\s*הקצאה\s*[:.]?\s*(\d{{6,12}})")
_DOCNUM_RE = re.compile(rf"(?:חשבונית(?:\s*מס)?(?:\s*קבלה)?|קבלה)\s*(?:מס{_Q}?|מספר)\s*[:.]?\s*(\d{{1,12}})")
_DATE_RES = [
    (re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"), ("y", "m", "d")),
    (re.compile(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})"), ("d", "m", "y")),
    (re.compile(r"(\d{1,2})[/.](\d{1,2})[/.](\d{2})(?!\d)"), ("d", "m", "yy")),
]
_TOTAL_RE = re.compile(rf"(?:סה{_Q}?כ\s*(?:לתשלום|כולל\s*מע{_Q}?מ)|לתשלום|סך\s*הכל\s*לתשלום|(?i:total)(?!\s*vat))\s*[:₪$€£]?\s*[₪$€£]?\s*{_NUM}")
_TOTAL_FALLBACK_RE = re.compile(rf"סה{_Q}?כ\s*[:₪$€£]?\s*[₪$€£]?\s*{_NUM}")
_PRE_RE = re.compile(rf"(?:לפני\s*מע{_Q}?מ|חייב\s*במע{_Q}?מ|סה{_Q}?כ\s*ללא\s*מע{_Q}?מ|(?i:subtotal|before\s*vat))\s*[:₪$€£]?\s*[₪$€£]?\s*{_NUM}")
_VAT_RE = re.compile(rf"(?<!לפני\s)(?<!ללא\s)(?<!במ)(?:מע{_Q}?מ|(?i:VAT)(?!\s*ID|\s*No))\s*(?:\(?\s*\d{{1,2}}(?:\.\d+)?\s*%\s*\)?)?\s*[:₪$€£]?\s*[₪$€£]?\s*{_NUM}")

_NOT_VENDOR = re.compile(rf"חשבונית|קבלה|תאריך|(?<![במ])ע{_Q}?מ\b|(?<![במ])ע\.מ|ח{_Q}?פ|ח\.פ|עוסק|סה{_Q}?כ|מע{_Q}?מ|לתשלום|הקצאה|^\W*$")


_CASH = re.compile(r"מזומן|\bcash\b", re.I)
_CARD = re.compile(r"אשראי|ויזה|מאסטרקארד|ישראכרט|אמריקן\s*אקספרס|\bvisa\b|master\s*card|credit\s*card", re.I)
_BANK = re.compile(r"העברה\s*בנקאית|בנק\s*הפועלים|\bbit\b|ביט\b|bank\s*transfer|wire", re.I)
_CHECK = re.compile(r"שיק|צ'ק|\bcheque?\b", re.I)
_FOREIGN_NAME = re.compile(r"\b(inc|ltd|llc|gmbh|corp|limited|plc|s\.?a\.?|b\.?v\.?)\b\.?", re.I)
_HEB = re.compile(r"[\u0590-\u05ff]")


def _payment_method(text: str) -> str:
    """אם מופיעים כמה אמצעים — האשראי/הבנק גוברים (מזומן מוזכר לעיתים כחלופה שלא נבחרה)."""
    if _CARD.search(text):
        return "credit_card"
    if _BANK.search(text):
        return "bank"
    if _CHECK.search(text):
        return "check"
    if _CASH.search(text):
        return "cash"
    return "unknown"


def _currency(text: str) -> str:
    if re.search(r"\$|\bUSD\b|דולר", text):
        return "USD"
    if re.search(r"€|\bEUR\b|אירו", text):
        return "EUR"
    if re.search(r"£|\bGBP\b|ליש\"ט", text):
        return "GBP"
    return "ILS"


def _num(s: str) -> float:
    return float(s.replace(",", "").replace(" ", ""))


def _doc_type(text: str) -> str:
    t = re.sub(r"\s+", " ", text)
    if re.search(r"חשבונית\s*זיכוי", t):
        return "credit_invoice"
    if re.search(r"חשבונית\s*מס\s*[/\-]?\s*קבלה", t):
        return "tax_invoice_receipt"
    if re.search(r"חשבונית\s*מס", t) or re.search(r"tax\s+invoice", t, re.I):
        return "tax_invoice"
    if re.search(r"קבלה", t) or re.search(r"\breceipt\b", t, re.I):
        return "receipt"
    return "unknown"


def _date(text: str) -> date | None:
    for rx, order in _DATE_RES:
        for m in rx.finditer(text):
            parts = dict(zip(order, m.groups()))
            year = int(parts["y"]) if "y" in parts else 2000 + int(parts["yy"])
            try:
                return date(year, int(parts["m"]), int(parts["d"]))
            except ValueError:
                continue
    return None


def _last(rx: re.Pattern, text: str) -> float | None:
    matches = rx.findall(text)
    return _num(matches[-1]) if matches else None


def extract(text: str) -> ExtractedDocument:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    vendor_name = next((ln for ln in lines[:5] if not _NOT_VENDOR.search(ln)), None)

    id_match = _ID_RE.search(text)
    vendor_id = re.sub(r"\D", "", id_match.group(1)) if id_match else None
    alloc = _ALLOC_RE.search(text)
    docnum = _DOCNUM_RE.search(text)

    total = _last(_TOTAL_RE, text)
    if total is None:
        total = _last(_TOTAL_FALLBACK_RE, text)
    pre = _last(_PRE_RE, text)
    vat = _last(_VAT_RE, text)

    line_items = [
        ln for ln in lines
        if ln != vendor_name and not _NOT_VENDOR.search(ln) and not re.fullmatch(r"[\d\s₪.,:/\-]+", ln)
    ]

    doc_type = _doc_type(text)
    doc_date = _date(text)
    currency = _currency(text)
    foreign = currency != "ILS" or bool(vendor_name and _FOREIGN_NAME.search(vendor_name) and not _HEB.search(vendor_name))
    found = [doc_type != "unknown", vendor_name, vendor_id, doc_date, total]
    confidence = 0.9 * sum(bool(x) for x in found) / len(found)
    # חשבונית מס בלי פירוט לפני מע"מ ומע"מ: אי אפשר לאמת את החישוב, ולכן לא מאשרים אוטומטית
    if doc_type in ("tax_invoice", "tax_invoice_receipt") and (pre is None or vat is None):
        confidence = min(confidence, 0.8)

    return ExtractedDocument(
        doc_type=doc_type,
        vendor_name=vendor_name,
        vendor_id=vendor_id,
        doc_number=docnum.group(1) if docnum else None,
        doc_date=doc_date,
        pre_vat_amount=pre,
        vat_amount=vat,
        total_amount=total,
        allocation_number=alloc.group(1) if alloc else None,
        currency=currency, payment_method=_payment_method(text), is_foreign_vendor=foreign,
        line_items=line_items[:20],
        extraction_confidence=round(confidence, 3),
    )
