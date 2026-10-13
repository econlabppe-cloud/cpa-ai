"""מחלץ מבוסס Claude (Anthropic) — טקסט, תמונה או PDF סרוק, עם פלט מובנה.

המודל מחלץ עובדות בלבד. שיעורי מס, ניכויים והחלטה על בדיקה אנושית נקבעים במנוע הכללים.
שליחה רק בהסכמת העסק (llm_consent). מפתח: ANTHROPIC_API_KEY. מודל: CPA_AI_CLAUDE_MODEL.
"""
from __future__ import annotations

import base64
import os
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel

from ..schemas import ExtractedDocument
from .openai_llm import SYSTEM_PROMPT

DEFAULT_MODEL = "claude-opus-5-5"


class ClaudeUnavailable(RuntimeError):
    pass


class ClaudeRefused(RuntimeError):
    pass


class _Extraction(BaseModel):
    """סכמה שטוחה לפלט המובנה (בלי אילוצי טווח, שאינם נתמכים בכל סכמה) — ממופה ל-ExtractedDocument."""
    doc_type: Literal["tax_invoice", "receipt", "tax_invoice_receipt", "credit_invoice", "unknown"]
    vendor_name: Optional[str]
    vendor_id: Optional[str]
    doc_number: Optional[str]
    doc_date: Optional[date]
    pre_vat_amount: Optional[float]
    vat_amount: Optional[float]
    total_amount: Optional[float]
    allocation_number: Optional[str]
    line_items: list[str]
    category_hint: Optional[str]
    currency: str
    payment_method: Literal["cash", "credit_card", "bank", "check", "unknown"]
    is_foreign_vendor: bool
    extraction_confidence: float


def _client():
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover - תלוי בהתקנה
        raise ClaudeUnavailable("חבילת anthropic אינה מותקנת (pip install anthropic)") from e
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        raise ClaudeUnavailable("ANTHROPIC_API_KEY אינו מוגדר")
    return anthropic.Anthropic()


def available() -> bool:
    try:
        _client()
        return True
    except ClaudeUnavailable:
        return False


def extract(text: str | None = None, image: bytes | None = None, mime: str = "image/jpeg",
            pdf: bytes | None = None, client=None) -> ExtractedDocument:
    client = client or _client()
    content: list[dict] = []
    if pdf:
        content.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                       "data": base64.standard_b64encode(pdf).decode()}})
    if image:
        content.append({"type": "image", "source": {"type": "base64", "media_type": mime,
                                                    "data": base64.standard_b64encode(image).decode()}})
    if text:
        content.append({"type": "text", "text": f"<document_text>\n{text}\n</document_text>"})
    if not content:
        raise ValueError("נדרש טקסט, תמונה או PDF")
    content.append({"type": "text", "text": "Extract the document fields."})

    response = client.messages.parse(
        model=os.getenv("CPA_AI_CLAUDE_MODEL", DEFAULT_MODEL),
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
        output_format=_Extraction,
        output_config={"effort": "low"},
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        # המסמך יישמר לבדיקה ידנית במקום להיכשל — ראו ingest
        raise ClaudeRefused("המודל לא החזיר חילוץ (סירוב או פלט לא תקין)")
    data = response.parsed_output.model_dump()
    data["extraction_confidence"] = min(max(data["extraction_confidence"], 0.0), 1.0)
    return ExtractedDocument(**data)
