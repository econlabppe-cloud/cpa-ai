"""נקודת כניסה ל-Vercel. Vercel דורש כאן `app` ברמה העליונה; הבנייה עצמה ב-cpa_ai/vercel_entry.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cpa_ai.vercel_entry import app  # noqa: E402,F401

__all__ = ["app"]
