from __future__ import annotations

import os

from ..schemas import ExtractedDocument
from . import claude_llm, heuristic, ocr, openai_llm

# מסמך שנקרא ב-OCR לעולם לא מאושר אוטומטית: הספרות הן הנקודה הרגישה ביותר בזיהוי תווים
OCR_CONFIDENCE_CAP = 0.8


def llm_provider():
    """(מודול, שם) של ספק ה-LLM הזמין, לפי CPA_AI_LLM_PROVIDER (auto/claude/openai), או (None, None)."""
    pref = os.getenv("CPA_AI_LLM_PROVIDER", "auto")
    if pref in ("auto", "claude") and claude_llm.available():
        return claude_llm, "claude"
    if pref in ("auto", "openai") and openai_llm.available():
        return openai_llm, "openai"
    return None, None


def extract_text(text: str, mode: str, llm_consent: bool) -> tuple[ExtractedDocument, str]:
    """מחזיר (חילוץ, שם המחלץ). שליחה ל-LLM חיצוני רק בהסכמת העסק."""
    if mode in ("openai", "claude"):
        if not llm_consent:
            raise PermissionError("העסק לא נתן הסכמה להעברת מסמכים לספק LLM חיצוני")
        mod = openai_llm if mode == "openai" else claude_llm
        return mod.extract(text=text), mode
    if mode == "auto" and llm_consent:
        mod, name = llm_provider()
        if mod is not None:
            return mod.extract(text=text), name
    return heuristic.extract(text), "heuristic"


def ocr_extract(text: str) -> ExtractedDocument:
    e = heuristic.extract(text)
    return e.model_copy(update={"extraction_confidence": min(e.extraction_confidence, OCR_CONFIDENCE_CAP)})


def capabilities() -> dict:
    _, name = llm_provider()
    return {"ocr_local": ocr.available(), "llm_provider": name,
            "llm_reads_pdf": name == "claude", "llm_reads_images": name is not None}
