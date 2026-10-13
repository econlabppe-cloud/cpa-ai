"""OCR מקומי (Tesseract) לתמונות ול-PDF סרוק — בלי לשלוח את המסמך החוצה.

Tesseract עם מודל עברית (`heb`) מחזיר טקסט בסדר לוגי, שעובר למחלץ המקומי ולמנוע הכללים כמו
כל טקסט אחר. התקנה: `apt install tesseract-ocr tesseract-ocr-heb poppler-utils`.
כיבוי: `CPA_AI_OCR=off`. שפות: `CPA_AI_OCR_LANGS` (ברירת מחדל heb+eng).
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

MAX_PAGES = 3
TIMEOUT = 60


def available() -> bool:
    return os.getenv("CPA_AI_OCR", "auto") != "off" and shutil.which("tesseract") is not None


def _langs() -> str:
    return os.getenv("CPA_AI_OCR_LANGS", "heb+eng")


def image_to_text(data: bytes) -> str:
    """psm 6: גוש טקסט אחיד — מתאים לקבלות וחשבוניות. הפלט מנוקה מסימני כיווניות."""
    r = subprocess.run(["tesseract", "stdin", "stdout", "-l", _langs(), "--psm", "6"],
                       input=data, capture_output=True, timeout=TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(f"tesseract נכשל: {r.stderr.decode(errors='replace')[:200]}")
    text = r.stdout.decode("utf-8", errors="replace")
    return "".join(ch for ch in text if ch not in "‎‏‪‫‬‭‮").strip()


def _pdf_page_images(data: bytes) -> list[bytes]:
    """עמודי PDF כתמונות. pdftoppm (מדויק, 300dpi) אם קיים; אחרת התמונות המוטמעות בעמוד (סריקה טיפוסית)."""
    if shutil.which("pdftoppm"):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.pdf"
            src.write_bytes(data)
            subprocess.run(["pdftoppm", "-r", "300", "-png", "-f", "1", "-l", str(MAX_PAGES), str(src),
                            str(Path(tmp) / "p")], capture_output=True, timeout=TIMEOUT, check=True)
            return [p.read_bytes() for p in sorted(Path(tmp).glob("p*.png"))]
    from pypdf import PdfReader
    out: list[bytes] = []
    for page in PdfReader(io.BytesIO(data)).pages[:MAX_PAGES]:
        out.extend(img.data for img in page.images)
    return out


def pdf_to_text(data: bytes) -> str:
    return "\n".join(t for t in (image_to_text(img) for img in _pdf_page_images(data)) if t).strip()
