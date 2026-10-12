"""טקסט מ-PDF דיגיטלי (הפורמט הנפוץ של חשבוניות בישראל), כולל התמודדות עם עברית.

pypdf מחזיר עברית בסדר שונה לפי האופן שבו ה-PDF נוצר. במקום להניח הנחה אחת, נבנים כמה מועמדים
ונבחר זה שהמחלץ מצליח בו הכי טוב (ציון מבוסס שדות + עקביות חשבונית: לפני מע"מ + מע"מ = סה"כ).
PDF סרוק (ללא שכבת טקסט) מוחזר ריק — ההמשך הוא חילוץ בתמונה בהסכמה או הזנה ידנית.
"""
from __future__ import annotations

import io
import re

from ..extraction import heuristic

HEB = "֐-׿"
_has_heb = re.compile(f"[{HEB}]")


def _runs_reversed(text: str) -> str:
    """pypdf כבר הופך אותיות בתוך קטע עברי; כאן מתקנים רק את סדר הקטעים בשורה (עברית / מספר)."""
    out = []
    for ln in text.splitlines():
        # מספר עם רווח כמפריד אלפים הוא טוקן אחד
        ln = re.sub(r"(\d{1,3}) (\d{3}(?:[.,]\d+)?)", lambda m: m.group(1) + "\u0001" + m.group(2), ln)
        runs: list[tuple[str, list[str]]] = []
        for tok in ln.split():
            kind = "h" if _has_heb.search(tok) else "o"
            if kind == "h" and runs and runs[-1][0] == "h":
                runs[-1][1].append(tok)
            else:
                runs.append((kind, [tok]))
        parts = []
        for kind, toks in runs[::-1]:
            s = " ".join(toks)
            if kind == "h" and s.startswith(":"):
                s = s[1:] + ":"
            parts.append(s)
        out.append(" ".join(parts).replace("\u0001", " "))
    return "\n".join(out)


def _letters_reversed(text: str) -> str:
    """PDF שבו האותיות בכל קטע עברי שמורות הפוך."""
    text = re.sub(f"(?<=[{HEB}\"':.])(?=\\d)", " ", text)
    text = re.sub(f"(?<=\\d)(?=[{HEB}])", " ", text)
    rx = re.compile(f"[{HEB}][{HEB} \t\"'״׳:.\\-]*[{HEB}\"'״׳:.]|[{HEB}]")
    return rx.sub(lambda m: m.group(0)[::-1], text)


CANDIDATES = (("raw", lambda t: t), ("runs_reversed", _runs_reversed), ("letters_reversed", _letters_reversed))


def score_text(text: str) -> int:
    e = heuristic.extract(text)
    got = [e.pre_vat_amount, e.vat_amount, e.total_amount, e.vendor_id, e.doc_date]
    score = sum(x is not None for x in got) + (e.doc_type != "unknown")
    if None not in (e.pre_vat_amount, e.vat_amount, e.total_amount) and abs(
            e.pre_vat_amount + e.vat_amount - e.total_amount) <= 1:
        score += 2
    return score


FULL_SCORE = 8


def extract_pdf_text(data: bytes) -> tuple[str, dict]:
    """מחזיר (טקסט נבחר, מידע). reliable=True רק כשכל השדות נמצאו והחשבונית עקבית."""
    from pypdf import PdfReader
    try:
        raw = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    except Exception as e:  # PDF פגום או מוצפן
        return "", {"candidate": None, "score": 0, "reliable": False, "reason": f"PDF לא נקרא: {type(e).__name__}"}
    if not raw.strip():
        return "", {"candidate": None, "score": 0, "reliable": False, "reason": "אין שכבת טקסט (כנראה סרוק)"}

    best_name, best_text, best_score = "raw", raw, score_text(raw)
    for name, fn in CANDIDATES[1:]:
        t = fn(raw)
        sc = score_text(t)
        if sc > best_score:
            best_name, best_text, best_score = name, t, sc
    return best_text, {"candidate": best_name, "score": best_score, "reliable": best_score >= FULL_SCORE,
                       "reason": None if best_score >= FULL_SCORE else "החילוץ מ-PDF אינו מלא או אינו עקבי"}
