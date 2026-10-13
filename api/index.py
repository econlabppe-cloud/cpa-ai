"""נקודת כניסה ל-Vercel (Python serverless). ההגדרות כולן ממשתני סביבה — ראו README, סעיף Vercel."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("CPA_AI_STORAGE", "db")          # אין דיסק קבוע ב-Vercel
os.environ.setdefault("CPA_AI_OCR", "off")              # אין Tesseract בסביבת הפונקציה

from cpa_ai.api import create_app  # noqa: E402

app = create_app()
