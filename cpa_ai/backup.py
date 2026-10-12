"""גיבוי ושחזור: מסד SQLite + קבצי מקור, עם manifest וגיבוב לכל רכיב.

הגיבוי של המסד נעשה דרך ה-API של SQLite (snapshot עקבי גם כשהאפליקציה כותבת). אם הוגדר מפתח הצפנה,
המסד בתוך הגיבוי מוצפן; קבצי המקור מועתקים כפי שהם (מוצפנים כבר אם הופעלה הצפנה).
PostgreSQL: להשתמש ב-pg_dump (לא ממומש כאן).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import storage


class BackupError(RuntimeError):
    pass


def _sqlite_path(db_url: str) -> Path:
    if not db_url.startswith("sqlite"):
        raise BackupError("גיבוי אוטומטי נתמך רק ל-SQLite. ל-PostgreSQL להשתמש ב-pg_dump")
    path = db_url.split("///", 1)[1] if "///" in db_url else ""
    if not path or path == ":memory:":
        raise BackupError("מסד נתונים בזיכרון אינו ניתן לגיבוי")
    return Path(path)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def create_backup(db_url: str, data_dir: Path, out_path: Path) -> dict:
    src = _sqlite_path(db_url)
    if not src.exists():
        raise BackupError(f"קובץ המסד לא נמצא: {src}")
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "snap.db"
        a, b = sqlite3.connect(src), sqlite3.connect(snap)
        try:
            a.backup(b)  # snapshot עקבי
        finally:
            b.close(); a.close()
        db_bytes = snap.read_bytes()
    f = storage.fernet()
    if f:
        db_bytes = f.encrypt(db_bytes)

    entries = {"db.sqlite": _sha(db_bytes)}
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("db.sqlite", db_bytes)
        base = Path(data_dir)
        if base.exists():
            for p in sorted(base.rglob("*")):
                if p.is_file():
                    raw = p.read_bytes()
                    name = "files/" + p.relative_to(base).as_posix()
                    z.writestr(name, raw)
                    entries[name] = _sha(raw)
        manifest = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "db_encrypted": f is not None,
                    "entries": entries}
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def verify_backup(zip_path: Path) -> list[str]:
    """רשימת בעיות; ריקה = תקין."""
    problems: list[str] = []
    try:
        with zipfile.ZipFile(zip_path) as z:
            manifest = json.loads(z.read("manifest.json"))
            for name, digest in manifest["entries"].items():
                try:
                    if _sha(z.read(name)) != digest:
                        problems.append(f"{name}: הגיבוב אינו תואם (הקובץ שונה או פגום)")
                except KeyError:
                    problems.append(f"{name}: חסר בגיבוי")
            extra = set(z.namelist()) - set(manifest["entries"]) - {"manifest.json"}
            problems += [f"{n}: קובץ שאינו ב-manifest" for n in sorted(extra)]
    except (zipfile.BadZipFile, KeyError, ValueError) as e:
        problems.append(f"הגיבוי אינו קריא: {e}")
    return problems


def restore_backup(zip_path: Path, db_path: Path, data_dir: Path, overwrite: bool = False) -> dict:
    problems = verify_backup(zip_path)
    if problems:
        raise BackupError("הגיבוי פגום, השחזור בוטל: " + "; ".join(problems[:3]))
    db_path, data_dir = Path(db_path), Path(data_dir)
    if not overwrite and (db_path.exists() or (data_dir.exists() and any(data_dir.iterdir()))):
        raise BackupError("היעד קיים. כדי לדרוס יש להשתמש ב-overwrite (הפעולה מוחקת את הנתונים הקיימים)")
    with zipfile.ZipFile(zip_path) as z:
        manifest = json.loads(z.read("manifest.json"))
        db_bytes = z.read("db.sqlite")
        if manifest.get("db_encrypted"):
            f = storage.fernet()
            if f is None:
                raise BackupError("הגיבוי מוצפן — נדרש CPA_AI_ENCRYPTION_KEY (המפתח שבו נוצר)")
            try:
                db_bytes = f.decrypt(db_bytes)
            except Exception as e:
                raise BackupError("מפתח ההצפנה אינו מתאים לגיבוי") from e
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_bytes(db_bytes)
        n = 0
        for name in z.namelist():
            if name.startswith("files/"):
                target = (data_dir / name[len("files/"):]).resolve()
                if data_dir.resolve() not in target.parents:
                    raise BackupError(f"נתיב לא חוקי בגיבוי: {name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(name))
                n += 1
    return {"files": n}
