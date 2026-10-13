"""נקודת כניסה ל-Vercel (Python serverless). ההגדרות כולן ממשתני סביבה — ראו README, סעיף Vercel."""
import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("CPA_AI_STORAGE", "db")          # אין דיסק קבוע ב-Vercel
os.environ.setdefault("CPA_AI_OCR", "off")              # אין Tesseract בסביבת הפונקציה


def _missing() -> list[str]:
    out = []
    if not (os.getenv("CPA_AI_USERS") or os.getenv("CPA_AI_API_KEY")):
        out.append("CPA_AI_USERS (או CPA_AI_API_KEY) — מפתח הכניסה")
    if not os.getenv("CPA_AI_DATABASE_URL"):
        out.append("CPA_AI_DATABASE_URL — כתובת PostgreSQL (ב-Vercel אין דיסק קבוע ל-SQLite)")
    return out


try:
    from cpa_ai.api import create_app
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
