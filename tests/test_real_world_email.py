"""מיילים אמיתיים: לוגואים, ניוזלטרים, וגרסת SQLite על קובץ."""
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from cpa_ai.api import create_app

from .helpers import make_pdf

H = {"X-API-Key": "k", "X-Actor": "t"}
RFC822 = {"Content-Type": "message/rfc822"}
PNG_LOGO = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1200  # לוגו קטן בחתימה


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_AI_DATA_DIR", str(tmp_path / "data"))


def make(client_token, msg_id, body, atts=()):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = "ספק@x.co", f"inbox+{client_token}@example.com", "נושא", msg_id
    m.set_content(body)
    for name, mt, st, data in atts:
        m.add_attachment(data, maintype=mt, subtype=st, filename=name)
    return m.as_bytes()


@pytest.fixture
def setup(tmp_path):
    c = TestClient(create_app(f"sqlite:///{tmp_path}/real.db", api_key="k"))  # קובץ, לא זיכרון
    bid = c.post("/api/v1/businesses", headers=H, json={"name": "א", "vat_id": "037544129", "owner_name": "ב"}).json()["id"]
    return c, bid, c.get(f"/api/v1/businesses/{bid}/connections", headers=H).json()["email"]["token"]


def post(c, raw):
    return c.post("/api/v1/inbound/email", content=raw, headers=RFC822).json()


def test_signature_logo_next_to_an_invoice_pdf_does_not_create_a_second_document(setup):
    c, bid, tok = setup
    raw = make(tok, "<a@x>", "מצורפת חשבונית", [("invoice.pdf", "application", "pdf", make_pdf()),
                                               ("image001.png", "image", "png", PNG_LOGO)])
    assert len(post(c, raw)["documents"]) == 1


def test_email_with_only_a_small_image_creates_nothing(setup):
    c, bid, tok = setup
    r = post(c, make(tok, "<b@x>", "תודה רבה", [("logo.png", "image", "png", PNG_LOGO)]))
    assert r["documents"] == [] and r["notes"]


def test_newsletter_with_a_total_in_the_body_is_not_treated_as_an_invoice(setup):
    c, bid, tok = setup
    r = post(c, make(tok, "<c@x>", 'מבצע! סה"כ לתשלום: 99.00 בלבד. להצטרפות לחצו כאן.'))
    assert r["documents"] == []


def test_explicit_invoice_text_in_email_body_is_still_ingested(setup):
    c, bid, tok = setup
    body = "חשבונית מס מס' 55\nח.פ 513456780\n15/07/2026\nסה\"כ לפני מע\"מ: 100.00\nמע\"מ 18%: 18.00\nסה\"כ לתשלום: 118.00\n"
    assert len(post(c, make(tok, "<d@x>", body))["documents"]) == 1


def test_small_image_sent_by_the_user_is_still_kept_for_manual_entry(setup):
    c, bid, tok = setup
    r = c.post(f"/api/v1/businesses/{bid}/documents/upload", headers=H, files=[("files", ("r.png", PNG_LOGO, "image/png"))])
    assert len(r.json()["documents"]) == 1  # העלאה יזומה של המשתמש — לא מסננים


def test_retry_is_idempotent_on_a_real_sqlite_file(setup):
    c, bid, tok = setup
    raw = make(tok, "<e@x>", "x", [("invoice.pdf", "application", "pdf", make_pdf())])
    assert len(post(c, raw)["documents"]) == 1
    assert post(c, raw)["skipped"] is True
    assert len(c.get(f"/api/v1/businesses/{bid}/documents", headers=H).json()) == 1
