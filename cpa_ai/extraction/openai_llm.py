"""מחלץ מבוסס OpenAI Structured Outputs.

המודל מחלץ עובדות בלבד (`ExtractedDocument`). אין בו שיעורי מס, אין נימוק
משפטי ואין החלטה על בדיקה אנושית — כל אלה במנוע הכללים.
"""
from __future__ import annotations

import base64
import os

from ..schemas import ExtractedDocument

SYSTEM_PROMPT = """You extract data from Israeli business documents (invoices, receipts).
Return ONLY facts printed on the document. Never compute tax treatment, never infer
missing amounts, never guess a business number. Leave a field null if it is not printed.
- doc_type: "tax_invoice" (חשבונית מס), "tax_invoice_receipt" (חשבונית מס/קבלה),
  "receipt" (קבלה), "credit_invoice" (חשבונית זיכוי), otherwise "unknown".
- vendor_id: digits only of ע.מ / ח.פ of the ISSUER (not the customer).
- allocation_number: the "מספר הקצאה" if printed.
- category_hint: one of office_supplies, software, inventory, professional_services,
  vehicle, refreshments, hospitality, equipment, unknown. It is only a hint.
- extraction_confidence: how sure you are the fields were read correctly (0-1)."""


class OpenAIUnavailable(RuntimeError):
    pass


def _client():
    try:
        import openai
    except ImportError as e:  # pragma: no cover - תלוי בהתקנה
        raise OpenAIUnavailable("חבילת openai אינה מותקנת (pip install openai)") from e
    if not os.getenv("OPENAI_API_KEY"):
        raise OpenAIUnavailable("OPENAI_API_KEY אינו מוגדר")
    return openai.OpenAI()


def available() -> bool:
    try:
        _client()
        return True
    except OpenAIUnavailable:
        return False


def extract(text: str | None = None, image: bytes | None = None, mime: str = "image/jpeg") -> ExtractedDocument:
    client = _client()
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": f"Document text:\n{text}"})
    if image:
        b64 = base64.b64encode(image).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    if not content:
        raise ValueError("נדרש טקסט או תמונה")

    completions = client.chat.completions
    parse = getattr(completions, "parse", None) or client.beta.chat.completions.parse
    response = parse(
        model=os.getenv("CPA_AI_OPENAI_MODEL", "gpt-4o"),
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}],
        response_format=ExtractedDocument,
        temperature=0.0,
    )
    parsed = response.choices[0].message.parsed
    if parsed is None:
        raise RuntimeError("המודל סירב או החזיר פלט שאינו תואם לסכמה")
    return parsed
