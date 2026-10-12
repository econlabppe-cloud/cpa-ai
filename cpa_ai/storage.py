"""אחסון קבצי מקור (חשבוניות, קבלות). נדרש לשמירת מסמכי מקור לפי הוראות ניהול פנקסים.

הקבצים נשמרים לפי sha256 — אותו קובץ שמגיע פעמיים נשמר פעם אחת.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


def data_dir() -> Path:
    return Path(os.getenv("CPA_AI_DATA_DIR", "./cpa_ai_data")).resolve()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(data: bytes, filename: str) -> tuple[str, str]:
    """מחזיר (נתיב יחסי, sha256)."""
    digest = sha256(data)
    ext = Path(filename).suffix.lower()[:10]
    if not ext.replace(".", "").isalnum():
        ext = ""
    rel = f"attachments/{digest[:2]}/{digest}{ext}"
    target = data_dir() / rel
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return rel, digest


def read(rel: str) -> bytes:
    root = data_dir()
    target = (root / rel).resolve()
    if root not in target.parents:  # הגנה מפני path traversal
        raise ValueError("נתיב קובץ לא חוקי")
    return target.read_bytes()
