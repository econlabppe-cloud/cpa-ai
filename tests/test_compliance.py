"""חוקיות ובטיחות קבצים: מזומן, מטבע חוץ/ספק זר, וסוג קובץ לפי תוכן ולא לפי מה שהלקוח טוען."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from cpa_ai.api import create_app
from cpa_ai.extraction import heuristic
from cpa_ai.rules import data
from cpa_ai.rules.engine import evaluate_expense
from cpa_ai.schemas import ExtractedDocument

H = {"X-API-Key": "k", "X-Actor": "t"}
VENDOR = "513456780"


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_AI_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def c():
    client = TestClient(create_app("sqlite://", api_key="k"))
    bid = client.post("/api/v1/businesses", headers=H, json={"name": "א", "vat_id": "037544129", "owner_name": "ב"}).json()["id"]
    return client, bid


def doc(**kw) -> ExtractedDocument:
    base = dict(doc_type="tax_invoice", vendor_name="אופיס דיפו", vendor_id=VENDOR, doc_date=date(2026, 7, 15),
                pre_vat_amount=1000.0, vat_amount=180.0, total_amount=1180.0, line_items=["נייר A4"],
                extraction_confidence=0.95)
    base.update(kw)
    return ExtractedDocument(**base)


def codes(d):
    return {f["code"] for f in d.findings}


# ── חילוץ: אמצעי תשלום ומטבע ───────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("שולם במזומן", "cash"), ("שולם בכרטיס אשראי ויזה", "credit_card"), ("העברה בנקאית", "bank"),
    ("Payment: cash", "cash"), ("שולם במזומן ובכרטיס אשראי", "credit_card"), ("ללא פירוט", "unknown"),
])
def test_payment_method_detection(text, expected):
    e = heuristic.extract(f'ספק\nח.פ {VENDOR}\nחשבונית מס מס\' 5\n10/07/2026\n{text}\nסה"כ לתשלום: 100.00\n')
    assert e.payment_method == expected


def test_foreign_currency_and_vendor_detection():
    e = heuristic.extract("Google Ireland Ltd\nTax Invoice No. 5\n10/07/2026\nGoogle Workspace\nTotal $120.00\n")
    assert e.currency == "USD" and e.is_foreign_vendor and e.total_amount == 120.0


def test_israeli_invoice_is_not_foreign():
    e = heuristic.extract('אופיס דיפו בע"מ\nח.פ 513456780\nחשבונית מס מס\' 5\n10/07/2026\nסה"כ לתשלום: 100.00\n')
    assert e.currency == "ILS" and not e.is_foreign_vendor


# ── כללים: מזומן ───────────────────────────────────────────────────────────
def test_cash_over_the_limit_is_flagged_and_sent_to_review():
    limit = data.CASH_LIMIT.value
    d = evaluate_expense(doc(payment_method="cash", pre_vat_amount=limit, vat_amount=limit * 0.18, total_amount=limit * 1.18))
    assert "CASH_LIMIT" in codes(d) and d.needs_human_review
    msg = next(f for f in d.findings if f["code"] == "CASH_LIMIT")["message"]
    assert "15%" in msg and "30%" in msg  # הקנס מפורש בהודעה


def test_small_cash_payment_is_fine():
    d = evaluate_expense(doc(payment_method="cash"))
    assert "CASH_LIMIT" not in codes(d) and not d.needs_human_review


def test_card_payment_over_limit_is_fine():
    d = evaluate_expense(doc(payment_method="credit_card", pre_vat_amount=50_000, vat_amount=9_000, total_amount=59_000,
                             allocation_number="123456789"))
    assert "CASH_LIMIT" not in codes(d)


def test_cash_limit_parameter_is_marked_unverified_with_the_conflict():
    assert data.CASH_LIMIT.source.verified is False and "11,000" in data.CASH_LIMIT.source.note


# ── כללים: ספק זר / מטבע חוץ ───────────────────────────────────────────────
def test_foreign_invoice_has_no_vendor_id_error_and_no_vat_deduction_but_needs_review():
    d = evaluate_expense(ExtractedDocument(
        doc_type="tax_invoice", vendor_name="Google Ireland Ltd", vendor_id=None, doc_date=date(2026, 7, 10),
        total_amount=120.0, currency="USD", is_foreign_vendor=True, line_items=["Google Workspace"],
        extraction_confidence=0.9))
    assert "VENDOR_ID_MISSING" not in codes(d) and "VAT_ZERO_ON_TAX_INVOICE" not in codes(d)
    assert "FOREIGN_CURRENCY" in codes(d) and "FOREIGN_VAT_NOTE" in codes(d)
    assert d.deductible_vat == 0.0 and d.needs_human_review


def test_foreign_invoice_after_ils_conversion_is_accepted_without_vendor_id():
    d = evaluate_expense(ExtractedDocument(
        doc_type="tax_invoice", vendor_name="Google Ireland Ltd", vendor_id=None, doc_date=date(2026, 7, 10),
        total_amount=440.0, currency="ILS", is_foreign_vendor=True, line_items=["Google Workspace"],
        extraction_confidence=0.9))
    assert "FOREIGN_CURRENCY" not in codes(d) and d.recognized_expense == 440.0
    assert not any(f["severity"] == "error" for f in d.findings)


# ── קבצים: סוג לפי תוכן ────────────────────────────────────────────────────
def upload(client, bid, name, content, mime):
    return client.post(f"/api/v1/businesses/{bid}/documents/upload", headers=H, files=[("files", (name, content, mime))])


HTML_INVOICE = ('<html><body><script>fetch("/steal?k="+localStorage.cpa_key)</script>'
                '<p>חשבונית מס מס\' 77</p><p>ח.פ 513456780</p><p>תאריך: 15/07/2026</p>'
                '<p>סה"כ לפני מע"מ: 1000.00</p><p>מע"מ 18%: 180.00</p><p>סה"כ לתשלום: 1180.00</p></body></html>').encode()


def test_html_uploaded_as_pdf_is_never_served_as_html(c):
    client, bid = c
    r = upload(client, bid, "invoice.pdf", HTML_INVOICE, "application/pdf")  # מתחזה ל-PDF
    ids = r.json()["documents"]
    assert len(ids) == 1
    f = client.get(f"/api/v1/documents/{ids[0]}/file", headers=H)
    assert f.status_code == 200
    assert f.headers["content-type"].split(";")[0] == "text/plain"
    assert f.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in f.headers["content-security-policy"]


def test_client_declared_mime_is_ignored_for_the_stored_type(c):
    client, bid = c
    r = upload(client, bid, "x.html", HTML_INVOICE, "text/html")
    f = client.get(f"/api/v1/documents/{r.json()['documents'][0]}/file", headers=H)
    assert f.headers["content-type"].split(";")[0] == "text/plain"


def test_binary_garbage_named_pdf_is_rejected(c):
    client, bid = c
    r = upload(client, bid, "evil.pdf", b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 100, "application/pdf")
    assert r.json()["documents"] == [] and r.json()["notes"]


def test_svg_with_script_is_not_served_as_an_image(c):
    client, bid = c
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    r = upload(client, bid, "r.svg", svg, "image/svg+xml")
    assert r.json()["documents"] == []  # אין חשבונית בטקסט הזה; ובכל מקרה לא נשמר כתמונה
