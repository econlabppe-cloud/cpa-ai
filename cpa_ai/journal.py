"""יומן פקודות כפול (חובה/זכות) מהמסמכים המאושרים — לייבוא לתוכנת הנהלת חשבונות של המשרד.

מפתח חשבונות פשוט ויציב (ניתן למיפוי בצד המקבל). כל מסמך הוא פקודה מאוזנת.
- הוצאה: חובה הוצאה מוכרת לפי קטגוריה (או רכוש קבוע), חובה מע"מ תשומות, חובה הוצאה לא מוכרת; זכות ספקים.
- הכנסה: חובה לקוחות (ולקוחות-ניכוי במקור); זכות הכנסות ומע"מ עסקאות.
- חשבונית זיכוי: אותה פקודה בכיוון הפוך.
"""
from __future__ import annotations

import csv
import io
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Document
from .package import _cell
from .rules import data

ACCOUNTS = {
    "customers": ("1200", "לקוחות"),
    "withholding": ("1310", "מס הכנסה — ניכוי במקור"),
    "input_vat": ("1410", "מע\"מ תשומות"),
    "fixed_assets": ("1500", "רכוש קבוע"),
    "suppliers": ("2000", "ספקים"),
    "output_vat": ("2210", "מע\"מ עסקאות"),
    "revenue": ("4000", "הכנסות"),
    "nondeductible": ("6990", "הוצאות לא מוכרות"),
}
CATEGORY_CODES = {key: f"6{100 + i:03d}" for i, key in enumerate(sorted(data.CATEGORIES))}


def _lines(d: Document) -> list[tuple[str, str, float, float]]:
    """(קוד, שם, חובה, זכות) למסמך אחד."""
    dec, ext = d.decision, d.extracted
    sign = -1 if ext.get("doc_type") == "credit_invoice" else 1
    out = []

    def add(code_name, amount, side):
        amount = round(amount, 2)
        if abs(amount) < 0.005:
            return
        if sign * amount < 0:  # זיכוי: הצד מתהפך
            side = "credit" if side == "debit" else "debit"
        v = abs(amount)
        out.append((code_name[0], code_name[1], v if side == "debit" else 0.0, v if side == "credit" else 0.0))

    if d.direction == "income":
        pre, vat = dec.get("pre_vat_amount") or 0.0, dec.get("vat_amount") or 0.0
        wh = ext.get("withholding_tax") or 0.0
        add(ACCOUNTS["customers"], pre + vat - wh, "debit")
        add(ACCOUNTS["withholding"], wh, "debit")
        add(ACCOUNTS["revenue"], pre, "credit")
        add(ACCOUNTS["output_vat"], vat, "credit")
    else:
        total = dec.get("total_amount") or 0.0
        ded_vat = abs(dec.get("deductible_vat") or 0.0)
        recognized = abs(dec.get("recognized_expense") or 0.0)
        cat = dec.get("category") or "unknown"
        if dec.get("is_fixed_asset"):
            account = ACCOUNTS["fixed_assets"]
        else:
            account = (CATEGORY_CODES.get(cat, "6999"), dec.get("category_label") or cat)
        add(account, recognized, "debit")
        add(ACCOUNTS["input_vat"], ded_vat, "debit")
        add(ACCOUNTS["nondeductible"], total - recognized - ded_vat, "debit")
        add(ACCOUNTS["suppliers"], total, "credit")
    return out


def journal_rows(s: Session, business_id: int, start: date, end: date) -> list[dict]:
    rows = []
    for d in s.scalars(select(Document).where(
            Document.business_id == business_id, Document.status == "confirmed",
            Document.doc_date >= start, Document.doc_date <= end).order_by(Document.doc_date, Document.id)):
        party = d.extracted.get("vendor_name") or d.extracted.get("customer_name") or ""
        desc = f"{d.extracted.get('doc_number') or ''} {party}".strip()
        for code, name, debit, credit in _lines(d):
            rows.append({"date": d.doc_date.isoformat(), "document_id": d.id, "doc_type": d.extracted.get("doc_type"),
                         "account_code": code, "account_name": name, "debit": debit, "credit": credit,
                         "description": desc})
    return rows


def journal_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    cols = ["date", "document_id", "doc_type", "account_code", "account_name", "debit", "credit", "description"]
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([_cell(r[c]) if isinstance(r[c], str) else (f"{r[c]:.2f}" if isinstance(r[c], float) else r[c])
                    for c in cols])
    return ("﻿" + buf.getvalue()).encode("utf-8")
