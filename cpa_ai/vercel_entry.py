"""בניית האפליקציה ל-Vercel. ההגדרות כולן ממשתני סביבה — ראו README, סעיף Vercel.

הלוגיקה כאן ולא ב-api/index.py כי Vercel מחפש שם `app` בניתוח סטטי של הקובץ (בלי להריץ אותו)."""
import os
import traceback

os.environ.setdefault("CPA_AI_STORAGE", "db")          # אין דיסק קבוע ב-Vercel
os.environ.setdefault("CPA_AI_OCR", "off")              # אין Tesseract בסביבת הפונקציה


def _missing_auth() -> bool:
    return not (os.getenv("CPA_AI_USERS") or os.getenv("CPA_AI_API_KEY"))


def _missing() -> list[str]:
    out = []
    if not (os.getenv("CPA_AI_USERS") or os.getenv("CPA_AI_API_KEY")):
        out.append("CPA_AI_USERS (או CPA_AI_API_KEY) — מפתח הכניסה")
    if not os.getenv("CPA_AI_DATABASE_URL"):
        out.append("CPA_AI_DATABASE_URL — כתובת PostgreSQL (ב-Vercel אין דיסק קבוע ל-SQLite)")
    return out


try:
    from .api import create_app
    if _missing_auth():
        # אין מפתחות: מצב הדגמה על מסד זמני בזיכרון (גם אם הוגדר מסד — לעולם לא חושפים אותו בלי הרשאות)
        from . import demo
        app = create_app("sqlite://", demo=True)
        demo.seed(app.state.SessionLocal)
    else:
        app = create_app()
except Exception as exc:  # noqa: BLE001 — במקום 500 סתמי מציגים מה חסר; הפרטים המלאים רק בלוג של Vercel
    traceback.print_exc()
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse
    app = FastAPI()
    _hint = ("המערכת לא עלתה בגלל הגדרה חסרה או שגויה.\n\n"
             + ("חסרים משתני סביבה:\n- " + "\n- ".join(_missing()) + "\n\n" if _missing() else "")
             + f"סוג השגיאה: {type(exc).__name__}\n"
             "הפרטים המלאים בלוג: Vercel → Deployments → ה-deploy האחרון → Functions → Logs.\n"
             "אחרי שינוי משתנים צריך Redeploy.")

    @app.get("/{path:path}")
    def _broken(path: str = ""):
        return PlainTextResponse(_hint, status_code=503)


def _restore_original_path(application) -> None:
    """Vercel מעביר את הבקשה כ-/api/index?__p=<הנתיב המקורי> (ראו vercel.json), כי חלק מהגרסאות מנתבות לפי
    יעד ה-rewrite. כאן משחזרים את הנתיב המקורי לפני הניתוב; אם הנתיב כבר מקורי — לא נוגעים בו."""
    from urllib.parse import parse_qsl, urlencode

    @application.middleware("http")
    async def restore_path(request, call_next):
        scope = request.scope
        if scope["path"] in ("/api/index", "/api/index/"):
            pairs = parse_qsl(scope.get("query_string", b"").decode(), keep_blank_values=True)
            original = next((v for k, v in pairs if k == "__p"), None)
            if original is not None:
                path = "/" + original.lstrip("/")
                scope["path"], scope["raw_path"] = path, path.encode()
                scope["query_string"] = urlencode([(k, v) for k, v in pairs if k != "__p"]).encode()
        return await call_next(request)


_restore_original_path(app)
