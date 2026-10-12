import base64
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from cpa_ai.api import create_app
from cpa_ai.integrations import email_imap
from cpa_ai.integrations.email_parser import parse_email, recipient_token

from .helpers import make_pdf

H = {"X-API-Key": "k", "X-Actor": "t"}
RFC822 = {"Content-Type": "message/rfc822"}


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_AI_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def client():
    return TestClient(create_app("sqlite://", api_key="k"))


@pytest.fixture
def biz(client):
    r = client.post("/api/v1/businesses", headers=H, json={"name": "א", "vat_id": "037544129", "owner_name": "ב"})
    bid = r.json()["id"]
    token = client.get(f"/api/v1/businesses/{bid}/connections", headers=H).json()["email"]["token"]
    return bid, token


def email_bytes(msg_id="<m1@x>", to="inbox+abcDEF123456@example.com", pdf=True, body="מצורפת חשבונית") -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = "ספק <billing@vendor.co.il>", to, "חשבונית מס", msg_id
    m.set_content(body)
    if pdf:
        m.add_attachment(make_pdf(), maintype="application", subtype="pdf", filename="invoice.pdf")
    return m.as_bytes()


def test_parse_email_extracts_attachment_body_and_ref():
    item, msg = parse_email(email_bytes())
    assert item.source_ref == "<m1@x>" and item.attachments[0].filename == "invoice.pdf"
    assert item.attachments[0].data.startswith(b"%PDF")
    assert recipient_token(msg) == "abcDEF123456"


def test_html_only_email_is_converted_to_text():
    from cpa_ai.ingest import html_to_text
    text = html_to_text("<html><body><p>חשבונית מס</p><p>סה\"כ לתשלום: 118.00</p></body></html>")
    assert "סה\"כ לתשלום: 118.00" in text and "<" not in text


def test_routed_webhook_ingests_pdf_and_is_idempotent(client, biz):
    bid, tok = biz
    raw = email_bytes(to=f"inbox+{tok}@example.com")
    r1 = client.post("/api/v1/inbound/email", content=raw, headers=RFC822)
    assert r1.status_code == 202 and len(r1.json()["documents"]) == 1
    assert client.post("/api/v1/inbound/email", content=raw, headers=RFC822).json()["skipped"] is True
    docs = client.get(f"/api/v1/businesses/{bid}/documents", headers=H).json()
    assert len(docs) == 1 and docs[0]["decision"]["total_amount"] == 1180.0 and docs[0]["status"] == "confirmed"


def test_routed_webhook_without_token_is_rejected(client, biz):
    r = client.post("/api/v1/inbound/email", content=email_bytes(to="nobody@example.com"), headers=RFC822)
    assert r.status_code == 404


def test_token_url_webhook_and_unknown_token(client, biz):
    _, tok = biz
    ok = client.post(f"/api/v1/inbound/email/{tok}", content=email_bytes(msg_id="<u1@x>"), headers=RFC822)
    assert ok.status_code == 202 and ok.json()["documents"]
    assert client.post("/api/v1/inbound/email/not-a-token", content=email_bytes(), headers=RFC822).status_code == 404


def test_json_webhook(client, biz):
    _, tok = biz
    payload = {"message_id": "pm-1", "from": "x@y.z", "subject": "חשבונית",
               "attachments": [{"filename": "i.pdf", "content_type": "application/pdf",
                                "content_base64": base64.b64encode(make_pdf()).decode()}]}
    r = client.post(f"/api/v1/inbound/email/{tok}/json", json=payload)
    assert r.status_code == 202 and len(r.json()["documents"]) == 1


def test_rotating_token_invalidates_the_old_one(client, biz):
    bid, old = biz
    assert client.post(f"/api/v1/businesses/{bid}/inbound-token/rotate", headers=H).status_code == 200
    assert client.post(f"/api/v1/inbound/email/{old}", content=email_bytes(), headers=RFC822).status_code == 404


def test_connections_requires_api_key(client, biz):
    bid, _ = biz
    assert client.get(f"/api/v1/businesses/{bid}/connections").status_code == 401


class FakeImap:
    def __init__(self, messages):
        self.messages, self.seen = messages, set()

    def __call__(self, host, port):  # משמש כ-imap_factory
        return self

    def login(self, u, p): return "OK", []
    def select(self, f): return "OK", []
    def search(self, _, crit): return "OK", [b" ".join(str(i).encode() for i in self.messages if i not in self.seen)]
    def fetch(self, num, _): return "OK", [(b"1", self.messages[int(num)])]
    def store(self, num, op, flag): self.seen.add(int(num))
    def logout(self): pass


def test_imap_poller_routes_by_plus_address_and_marks_seen(client, biz):
    _, tok = biz
    imap = FakeImap({1: email_bytes(msg_id="<i1@x>", to=f"inbox+{tok}@gmail.com"),
                     2: email_bytes(msg_id="<i2@x>", to="nobody@gmail.com")})
    cfg = email_imap.ImapConfig("h", "u", "p")
    stats = email_imap.poll_once(client.app.state.SessionLocal, cfg, imap)
    assert stats["fetched"] == 2 and stats["documents"] == 1 and stats["unrouted"] == 1
    assert imap.seen == {1}  # ההודעה הלא-מנותבת נשארת לא-נקראה עד שיוגדר ניתוב
    again = email_imap.poll_once(client.app.state.SessionLocal, cfg, imap)
    assert again["fetched"] == 1 and again["documents"] == 0 and again["unrouted"] == 1
