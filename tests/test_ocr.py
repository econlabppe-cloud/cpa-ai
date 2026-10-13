"""OCR מקומי וחילוץ בבינה מלאכותית לתמונות ול-PDF סרוק."""
from datetime import date
from types import SimpleNamespace as NS

import pytest

from cpa_ai.extraction import claude_llm, ocr
from cpa_ai.ingest import Attachment, ingest

from .helpers import make_image, scanned_pdf
from .test_ingest import item

needs_tesseract = pytest.mark.skipif(not ocr.available(), reason="tesseract לא מותקן")


@needs_tesseract
def test_photo_receipt_is_read_locally(env):
    s, biz = env
    d = ingest(s, biz, item("p", atts=[Attachment("r.png", "image/png", make_image())]), "t").documents[0]
    assert d.extractor == "ocr+heuristic"
    assert (d.decision["pre_vat_amount"], d.decision["vat_amount"], d.decision["total_amount"]) == (1000.0, 180.0, 1180.0)
    assert d.extracted["vendor_id"] == "513456780"
    # OCR לעולם לא מאושר אוטומטית, אבל הוא מוכן לאישור מרוכז
    assert d.status == "pending_review"
    from cpa_ai.home import bulk_ready
    assert bulk_ready(d)[0] is True


@needs_tesseract
def test_scanned_pdf_is_read_locally(env):
    s, biz = env
    d = ingest(s, biz, item("s", atts=[Attachment("scan.pdf", "application/pdf", scanned_pdf())]), "t").documents[0]
    assert d.extractor == "ocr+heuristic" and d.decision["total_amount"] == 1180.0


def test_without_ocr_or_llm_image_is_kept_for_manual_entry(env, monkeypatch):
    monkeypatch.setenv("CPA_AI_OCR", "off")
    s, biz = env
    d = ingest(s, biz, item("m", atts=[Attachment("r.png", "image/png", make_image())]), "t").documents[0]
    assert d.extractor == "image-manual" and d.status == "pending_review" and d.attachment_path


class FakeClaude:
    """תחליף ל-anthropic.Anthropic: רושם את הבקשה ומחזיר פלט מובנה."""
    def __init__(self, parsed=None, stop="end_turn"):
        self.calls, self.parsed, self.stop = [], parsed, stop
        self.messages = self

    def parse(self, **kw):
        self.calls.append(kw)
        return NS(stop_reason=self.stop, parsed_output=self.parsed)


PARSED = claude_llm._Extraction(
    doc_type="tax_invoice", vendor_name="אופיס דיפו", vendor_id="513456780", doc_number="1", doc_date=date(2026, 7, 15),
    pre_vat_amount=1000.0, vat_amount=180.0, total_amount=1180.0, allocation_number=None, line_items=["נייר"],
    category_hint="office_supplies", currency="ILS", payment_method="unknown", is_foreign_vendor=False,
    extraction_confidence=1.4)


def test_claude_reads_scanned_pdf_as_document_block():
    fake = FakeClaude(PARSED)
    e = claude_llm.extract(pdf=b"%PDF-1.4 x", client=fake)
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["messages"][0]["content"][0]["type"] == "document"
    assert call["output_format"] is claude_llm._Extraction
    assert "untrusted" in call["system"]
    assert e.total_amount == 1180.0 and e.extraction_confidence == 1.0


def test_claude_refusal_raises():
    with pytest.raises(claude_llm.ClaudeRefused):
        claude_llm.extract(text="x", client=FakeClaude(None, stop="refusal"))


def test_llm_failure_falls_back_to_ocr(env, monkeypatch):
    s, biz = env
    biz.llm_consent = True
    from cpa_ai import extraction

    def boom(**kw):
        raise RuntimeError("network")
    monkeypatch.setattr(extraction, "llm_provider", lambda: (NS(extract=boom), "claude"))
    monkeypatch.setattr(extraction.ocr, "available", lambda: True)
    monkeypatch.setattr(extraction.ocr, "image_to_text", lambda data: "\n".join([
        'אופיס דיפו בע"מ', "ח.פ 513456780", "חשבונית מס מס' 5", "תאריך: 15/07/2026",
        'סה"כ לפני מע"מ: 100.00', 'מע"מ 18%: 18.00', 'סה"כ לתשלום: 118.00']))
    d = ingest(s, biz, item("f", atts=[Attachment("r.png", "image/png", make_image())]), "t").documents[0]
    assert d.extractor == "ocr+heuristic" and d.decision["total_amount"] == 118.0


def test_image_uses_llm_only_with_consent(env, monkeypatch):
    s, biz = env
    from cpa_ai import extraction
    called = []
    monkeypatch.setattr(extraction, "llm_provider", lambda: (NS(extract=lambda **kw: called.append(kw) or
                                                                  claude_llm.ExtractedDocument(doc_type="unknown")), "claude"))
    monkeypatch.setenv("CPA_AI_OCR", "off")
    ingest(s, biz, item("c", atts=[Attachment("r.png", "image/png", make_image())]), "t")
    assert called == []  # בלי הסכמה — לא נשלח החוצה
