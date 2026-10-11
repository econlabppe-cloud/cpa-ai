from __future__ import annotations

from ..schemas import ExtractedDocument
from . import heuristic, openai_llm


def extract_text(text: str, mode: str, llm_consent: bool) -> tuple[ExtractedDocument, str]:
    """מחזיר (חילוץ, שם המחלץ). שליחה ל-LLM חיצוני רק בהסכמת העסק."""
    if mode == "openai":
        if not llm_consent:
            raise PermissionError("העסק לא נתן הסכמה להעברת מסמכים לספק LLM חיצוני")
        return openai_llm.extract(text=text), "openai"
    if mode == "auto" and llm_consent and openai_llm.available():
        return openai_llm.extract(text=text), "openai"
    return heuristic.extract(text), "heuristic"
