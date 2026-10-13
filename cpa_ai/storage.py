"""אחסון קבצי מקור (חשבוניות, קבלות). נדרש לשמירת מסמכי מקור לפי הוראות ניהול פנקסים.

הקבצים נשמרים לפי sha256 של התוכן הגלוי — אותו קובץ שמגיע פעמיים נשמר פעם אחת.
אם הוגדר CPA_AI_ENCRYPTION_KEY הם מוצפנים במנוחה (Fernet). אפשר כמה מפתחות מופרדים בפסיק: הראשון
מצפין, כולם מפענחים — כך מבצעים רוטציה בלי לאבד גישה לקבצים ישנים.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

MAGIC = b"CPAENC1\n"


class StorageError(RuntimeError):
    pass


_sessionmaker = None


def configure(sessionmaker) -> None:
    """נקרא ביצירת האפליקציה: מצב db משתמש באותו מסד כמו שאר הנתונים."""
    global _sessionmaker
    _sessionmaker = sessionmaker


def _db_mode() -> bool:
    return os.getenv("CPA_AI_STORAGE", "disk") == "db"


def data_dir() -> Path:
    return Path(os.getenv("CPA_AI_DATA_DIR", "./cpa_ai_data")).resolve()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fernet():
    """MultiFernet לפי CPA_AI_ENCRYPTION_KEY, או None אם לא הוגדר הצפנה."""
    keys = [k.strip() for k in os.getenv("CPA_AI_ENCRYPTION_KEY", "").split(",") if k.strip()]
    if not keys:
        return None
    try:
        from cryptography.fernet import Fernet, MultiFernet
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except ImportError as e:
        raise StorageError("הוגדר CPA_AI_ENCRYPTION_KEY אבל החבילה cryptography אינה מותקנת") from e
    except (ValueError, TypeError) as e:
        raise StorageError("מפתח ההצפנה לא תקין (נוצר ב-`python -m cpa_ai gen-key`)") from e


def save(data: bytes, filename: str) -> tuple[str, str]:
    """מחזיר (נתיב יחסי, sha256 של התוכן הגלוי)."""
    digest = sha256(data)
    ext = Path(filename).suffix.lower()[:10]
    if not ext.replace(".", "").isalnum():
        ext = ""
    rel = f"attachments/{digest[:2]}/{digest}{ext}"
    f = fernet()  # נבדק גם כשהקובץ קיים, כדי ששגיאת מפתח לא תיבלע
    if _db_mode():
        from .db import StoredFile
        with _sessionmaker() as s:
            if s.get(StoredFile, rel) is None:
                s.add(StoredFile(path=rel, data=MAGIC + f.encrypt(data) if f else data))
                s.commit()
        return rel, digest
    target = data_dir() / rel
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(MAGIC + f.encrypt(data) if f else data)
    return rel, digest


def read(rel: str) -> bytes:
    if _db_mode():
        from .db import StoredFile
        with _sessionmaker() as s:
            row = s.get(StoredFile, rel)
            if row is None:
                raise StorageError("הקובץ לא נמצא")
            blob = bytes(row.data)
    else:
        root = data_dir()
        target = (root / rel).resolve()
        if root not in target.parents:  # הגנה מפני path traversal
            raise ValueError("נתיב קובץ לא חוקי")
        blob = target.read_bytes()
    if not blob.startswith(MAGIC):
        return blob  # קובץ שנשמר לפני שהופעלה הצפנה
    f = fernet()
    if f is None:
        raise StorageError("הקובץ מוצפן אך לא הוגדר CPA_AI_ENCRYPTION_KEY")
    try:
        return f.decrypt(blob[len(MAGIC):])
    except Exception as e:
        raise StorageError("פענוח הקובץ נכשל — מפתח ההצפנה שגוי או שהקובץ פגום") from e
