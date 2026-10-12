import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from cpa_ai.api import create_app
from cpa_ai.integrations.whatsapp import WhatsAppClient, normalize_phone, parse_webhook, verify_signature

from .helpers import INVOICE_LINES, make_pdf

H = {"X-API-Key": "k", "X-Actor": "t"}
SECRET = "app-secret"
PHONE = "972501234567"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_AI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CPA_AI_WHATSAPP_APP_SECRET", SECRET)
    monkeypatch.setenv("CPA_AI_WHATSAPP_VERIFY_TOKEN", "verify-me")


class FakeGraph:
    """מדמה את ה-Graph API: שליפת מטא-דאטה של מדיה, הורדת הקובץ, ושליחת הודעות."""

    def __init__(self, files: dict):
        self.files, self.sent, self.calls = files, [], []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers.get("Authorization")))
        if method == "POST" and url.endswith("/messages"):
            self.sent.append(json.loads(body))
            return 200, b"{}"
        if "/download/" in url:
            return 200, self.files[url.rsplit("/", 1)[1]][0]
        media_id = url.rsplit("/", 1)[1]
        if media_id not in self.files:
            return 404, b"{}"
        return 200, json.dumps({"url": f"https://cdn.example/download/{media_id}",
                                "mime_type": self.files[media_id][1]}).encode()


@pytest.fixture
def setup():
    app = create_app("sqlite://", api_key="k")
    c = TestClient(app)
    bid = c.post("/api/v1/businesses", headers=H,
                 json={"name": "א", "vat_id": "037544129", "owner_name": "ב"}).json()["id"]
    assert c.put(f"/api/v1/businesses/{bid}/whatsapp-phone", headers=H, params={"phone": "050-123-4567"}).json() == {"phone": PHONE}
    graph = FakeGraph({"MEDIA1": (make_pdf(), "application/pdf")})
    app.state.whatsapp_client = WhatsAppClient("TOKEN", fetch=graph)
    return c, bid, graph


def payload(msg: dict, phone_number_id="PNID") -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": phone_number_id},
        "messages": [msg]}}]}]}


def post(c, body: dict, secret=SECRET, sign=True):
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if sign:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return c.post("/api/v1/inbound/whatsapp", content=raw, headers=headers)


def doc_msg(mid="wamid.1"):
    return {"id": mid, "from": PHONE, "type": "document",
            "document": {"id": "MEDIA1", "mime_type": "application/pdf", "filename": "inv.pdf"}}


def test_phone_normalization():
    assert normalize_phone("050-123-4567") == PHONE
    assert normalize_phone("+972 50 123 4567") == PHONE
    assert normalize_phone("00972501234567") == PHONE


def test_signature_check():
    raw = b'{"a":1}'
    good = "sha256=" + hmac.new(b"s", raw, hashlib.sha256).hexdigest()
    assert verify_signature(raw, good, "s")
    assert not verify_signature(raw, good, "other")
    assert not verify_signature(raw, None, "s")
    assert not verify_signature(raw, "sha1=abc", "s")


def test_challenge_verification(setup):
    c, _, _ = setup
    ok = c.get("/api/v1/inbound/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    assert c.get("/api/v1/inbound/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"}).status_code == 403


def test_unsigned_or_badly_signed_requests_are_rejected(setup):
    c, bid, _ = setup
    assert post(c, payload(doc_msg()), sign=False).status_code == 403
    assert post(c, payload(doc_msg()), secret="wrong").status_code == 403
    assert c.get(f"/api/v1/businesses/{bid}/documents", headers=H).json() == []


def test_endpoint_is_closed_when_secret_not_configured(setup, monkeypatch):
    c, _, _ = setup
    monkeypatch.delenv("CPA_AI_WHATSAPP_APP_SECRET")
    assert post(c, payload(doc_msg())).status_code == 503


def test_pdf_sent_in_whatsapp_becomes_a_document_and_gets_a_reply(setup):
    c, bid, graph = setup
    assert post(c, payload(doc_msg())).json() == {"status": "ok", "messages": 1}
    docs = c.get(f"/api/v1/businesses/{bid}/documents", headers=H).json()
    assert len(docs) == 1 and docs[0]["status"] == "confirmed"
    assert docs[0]["decision"]["total_amount"] == 1180.0
    # הקובץ ירד עם ה-Bearer, ונשלחה תשובה למספר השולח
    assert all(auth == "Bearer TOKEN" for _, _, auth in graph.calls)
    assert len(graph.sent) == 1 and graph.sent[0]["to"] == PHONE
    assert "✅" in graph.sent[0]["text"]["body"] and "₪1,180" in graph.sent[0]["text"]["body"]


def test_meta_retry_does_not_duplicate_or_reply_twice(setup):
    c, bid, graph = setup
    post(c, payload(doc_msg("wamid.R")))
    post(c, payload(doc_msg("wamid.R")))
    assert len(c.get(f"/api/v1/businesses/{bid}/documents", headers=H).json()) == 1
    assert len(graph.sent) == 1


def test_same_invoice_resent_as_new_message_is_reported_as_duplicate(setup):
    c, bid, graph = setup
    post(c, payload(doc_msg("wamid.A")))
    post(c, payload(doc_msg("wamid.B")))
    assert len(c.get(f"/api/v1/businesses/{bid}/documents", headers=H).json()) == 1
    assert "כבר קיבלתי" in graph.sent[1]["text"]["body"]


def test_text_invoice_in_chat_is_parsed(setup):
    c, bid, graph = setup
    post(c, payload({"id": "wamid.T", "from": PHONE, "type": "text", "text": {"body": "\n".join(INVOICE_LINES)}}))
    assert len(c.get(f"/api/v1/businesses/{bid}/documents", headers=H).json()) == 1


def test_unknown_sender_is_ignored_silently(setup):
    c, bid, graph = setup
    msg = doc_msg("wamid.X"); msg["from"] = "972509999999"
    post(c, payload(msg))
    assert c.get(f"/api/v1/businesses/{bid}/documents", headers=H).json() == [] and graph.sent == []


def test_expired_media_gets_a_helpful_reply_not_a_crash(setup):
    c, bid, graph = setup
    msg = doc_msg("wamid.E"); msg["document"]["id"] = "GONE"
    assert post(c, payload(msg)).status_code == 200
    assert "לא הצלחתי להוריד" in graph.sent[0]["text"]["body"]


def test_chat_message_that_is_not_an_invoice_gets_guidance(setup):
    c, bid, graph = setup
    post(c, payload({"id": "wamid.H", "from": PHONE, "type": "text", "text": {"body": "שלום, מה נשמע?"}}))
    assert graph.sent[0]["text"]["body"].startswith("לא זיהיתי חשבונית בהודעה")
    assert c.get(f"/api/v1/businesses/{bid}/documents", headers=H).json() == []


def test_voice_message_gets_help(setup):
    c, bid, graph = setup
    post(c, payload({"id": "wamid.V", "from": PHONE, "type": "audio", "audio": {"id": "A1"}}))
    assert "פקודות" in graph.sent[0]["text"]["body"]


def test_parse_webhook_ignores_status_updates():
    status_only = {"entry": [{"changes": [{"value": {"statuses": [{"id": "x", "status": "delivered"}]}}]}]}
    assert parse_webhook(status_only) == []
