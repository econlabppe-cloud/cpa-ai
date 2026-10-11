"""מחולל קבצים במבנה אחיד (INI.TXT + BKMVDATA.TXT) — טיוטה.

דרישת רישום תוכנה (נספח ה' להוראות מס הכנסה (ניהול פנקסי חשבונות)) כוללת
מודול להפקת קבצים במבנה אחיד. פריסת השדות כאן היא שלד ממוסגר בטבלה:
אורכי השדות וסדרם **לא אומתו** מול מפרט רשות המסים העדכני, ויש לתקף את הפלט
בסימולטור המבנה האחיד של רשות המסים לפני כל שימוש. תיקון = עריכת SPEC בלבד.
"""
from __future__ import annotations

import io
import zipfile
from datetime import date, datetime

SPEC_STATUS = "DRAFT — פריסת השדות לא אומתה מול מפרט רשות המסים"
SYSTEM_CONSTANT = "&OF1.31&"

# (שם שדה, אורך, סוג: 'X' טקסט מיושר לימין-מרופד ברווחים, 'N' מספר מרופד באפסים, 'A' סכום עם סימן)
SPEC: dict[str, list[tuple[str, int, str]]] = {
    "A100": [("code", 4, "X"), ("rec_no", 9, "N"), ("vat_id", 9, "N"), ("primary_id", 15, "N"),
             ("constant", 8, "X"), ("reserve", 50, "X")],
    "C100": [("code", 4, "X"), ("rec_no", 9, "N"), ("vat_id", 9, "N"), ("doc_type", 3, "N"),
             ("doc_number", 20, "X"), ("doc_date", 8, "N"), ("party_name", 50, "X"), ("party_vat_id", 9, "N"),
             ("amount_pre_vat", 15, "A"), ("vat_amount", 15, "A"), ("total", 15, "A"), ("reserve", 30, "X")],
    "Z900": [("code", 4, "X"), ("rec_no", 9, "N"), ("vat_id", 9, "N"), ("primary_id", 15, "N"),
             ("constant", 8, "X"), ("total_records", 15, "N"), ("reserve", 50, "X")],
}

# קודי סוג מסמך — טיוטה, לאימות מול נספח המפרט
DOC_TYPE_CODES = {"tax_invoice": 305, "tax_invoice_receipt": 320, "credit_invoice": 330, "receipt": 400}


def _fmt(value, length: int, kind: str) -> str:
    if kind == "N":
        s = str(int(value or 0))
        if len(s) > length:
            raise ValueError(f"ערך {s} ארוך מ-{length}")
        return s.zfill(length)
    if kind == "A":
        cents = int(round(float(value or 0) * 100))
        sign = "-" if cents < 0 else "+"
        return sign + str(abs(cents)).zfill(length - 1)
    s = "" if value is None else str(value)
    return s[:length].ljust(length)


def record(code: str, fields: dict) -> str:
    return "".join(_fmt(fields.get(name, code if name == "code" else None), ln, kind) for name, ln, kind in SPEC[code])


def build(vat_id: str, business_name: str, documents: list[dict], primary_id: int | None = None) -> bytes:
    """documents: [{direction, doc_type, doc_number, doc_date, party_name, party_vat_id, pre_vat, vat, total}]"""
    primary_id = primary_id or int(datetime.now().strftime("%y%m%d%H%M%S"))
    lines = [record("A100", {"rec_no": 1, "vat_id": vat_id, "primary_id": primary_id, "constant": SYSTEM_CONSTANT})]
    counts: dict[str, int] = {"C100": 0}
    for d in documents:
        if d["doc_type"] not in DOC_TYPE_CODES:
            continue
        doc_date = d["doc_date"]
        lines.append(record("C100", {
            "rec_no": len(lines) + 1, "vat_id": vat_id, "doc_type": DOC_TYPE_CODES[d["doc_type"]],
            "doc_number": d.get("doc_number") or "", "doc_date": doc_date.strftime("%Y%m%d") if isinstance(doc_date, date) else 0,
            "party_name": d.get("party_name") or "", "party_vat_id": d.get("party_vat_id") or 0,
            "amount_pre_vat": d.get("pre_vat"), "vat_amount": d.get("vat"), "total": d.get("total"),
        }))
        counts["C100"] += 1
    lines.append(record("Z900", {"rec_no": len(lines) + 1, "vat_id": vat_id, "primary_id": primary_id,
                                 "constant": SYSTEM_CONSTANT, "total_records": len(lines) + 1}))
    bkmv = "\r\n".join(lines) + "\r\n"
    ini = "\r\n".join([
        f"# {SPEC_STATUS}",
        f"VAT_ID={vat_id}", f"BUSINESS={business_name}", f"PRIMARY_ID={primary_id}",
        f"TOTAL_RECORDS={len(lines)}", *(f"{k}={v}" for k, v in counts.items()),
    ]) + "\r\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # המפרט הרשמי מגדיר קידוד ISO-8859-8 לעברית
        z.writestr("INI.TXT", ini.encode("iso-8859-8", errors="replace"))
        z.writestr("BKMVDATA.TXT", bkmv.encode("iso-8859-8", errors="replace"))
        z.writestr("README.txt", f"{SPEC_STATUS}\nיש לתקף בסימולטור המבנה האחיד של רשות המסים.\n".encode("utf-8"))
    return buf.getvalue()
