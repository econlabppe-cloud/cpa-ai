"""שורת פקודה:  python -m cpa_ai [serve | gen-key | backup | verify-backup | restore | migrate]"""
import argparse
import os
import sys
from pathlib import Path


def _serve() -> None:
    import uvicorn
    if not (os.getenv("CPA_AI_API_KEY") or os.getenv("CPA_AI_USERS") or os.getenv("CPA_AI_ALLOW_INSECURE") == "1"):
        sys.exit("הגדירו CPA_AI_API_KEY (או CPA_AI_USERS). לפיתוח מקומי בלבד: CPA_AI_ALLOW_INSECURE=1")
    uvicorn.run("cpa_ai.api:create_app", factory=True,
                host=os.getenv("CPA_AI_HOST", "127.0.0.1"), port=int(os.getenv("CPA_AI_PORT", "8000")))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="cpa_ai")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="הפעלת השרת (ברירת מחדל)")
    sub.add_parser("gen-key", help="יצירת מפתח הצפנה ל-CPA_AI_ENCRYPTION_KEY")
    b = sub.add_parser("backup", help="גיבוי מסד SQLite וקבצי מקור")
    b.add_argument("out")
    v = sub.add_parser("verify-backup", help="בדיקת שלמות גיבוי")
    v.add_argument("zip")
    r = sub.add_parser("restore", help="שחזור מגיבוי")
    r.add_argument("zip")
    r.add_argument("--db-path", required=True)
    r.add_argument("--data-dir", required=True)
    r.add_argument("--overwrite", action="store_true")
    sub.add_parser("migrate", help="הוספת עמודות חסרות למסד קיים")
    args = ap.parse_args(argv)

    if args.cmd in (None, "serve"):
        _serve()
        return 0
    if args.cmd == "gen-key":
        from cryptography.fernet import Fernet
        print(Fernet.generate_key().decode())
        return 0
    from . import backup, storage
    try:
        if args.cmd == "backup":
            url = os.getenv("CPA_AI_DATABASE_URL", "sqlite:///./cpa_ai.db")
            m = backup.create_backup(url, storage.data_dir(), Path(args.out))
            print(f"גיבוי נוצר: {args.out} ({len(m['entries'])} רכיבים, מסד מוצפן: {m['db_encrypted']})")
        elif args.cmd == "verify-backup":
            problems = backup.verify_backup(Path(args.zip))
            print("\n".join(problems) if problems else "הגיבוי תקין")
            return 1 if problems else 0
        elif args.cmd == "restore":
            res = backup.restore_backup(Path(args.zip), Path(args.db_path), Path(args.data_dir), args.overwrite)
            print(f"שוחזר: מסד + {res['files']} קבצים")
        elif args.cmd == "migrate":
            from .db import make_sessionmaker
            make_sessionmaker()
            print("המיגרציה הושלמה")
    except (backup.BackupError, storage.StorageError) as e:
        print(f"שגיאה: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
