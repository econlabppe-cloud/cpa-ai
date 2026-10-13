"""עבודה משותפת רו"ח–לקוח: פניות, שרשור, צירוף מסמכים, בקשת קבלות, כרטיס לקוח והתראות."""
import base64

import pytest
from fastapi.testclient import TestClient

from cpa_ai.api import create_app

from .helpers import make_pdf
from .test_rules import HEB_INVOICE

USERS = {
    "adm": "מנהל",
    "acc": {"name": "רו\"ח ישראלה", "role": "accountant", "businesses": [1, 2]},
    "dana": {"name": "דנה", "role": "client", "businesses": [1]},
    "moshe": {"name": "משה", "role": "client", "businesses": [2]},
}


def h(key):
    return {"X-API-Key": key}


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, **kw):
        self.sent.append(kw)
        return ["whatsapp"] if kw.get("phone") else []

    def channels(self):
        return {"whatsapp": True, "email": False}


@pytest.fixture
def c():
    app = create_app("sqlite://", users=USERS)
    app.state.notifier = FakeNotifier()
    cl = TestClient(app)
    for vat, name in (("037544129", "סטודיו דנה"), ("513451005", "משה בע\"מ")):
        assert cl.post("/api/v1/businesses", headers=h("adm"), json={"name": name, "vat_id": vat, "owner_name": "x"}).status_code == 201
    cl.put("/api/v1/businesses/1/whatsapp-phone", headers=h("adm"), params={"phone": "0501234567"})
    return cl


def test_accountant_asks_client_and_ball_moves(c):
    r = c.post("/api/v1/businesses/1/threads", headers=h("acc"),
               json={"kind": "question", "title": "מה זה החיוב של 900 ₪?", "body": "חיוב מ-3/9 בכרטיס"})
    assert r.status_code == 201, r.text
    th = r.json()
    assert th["waiting_on"] == "client" and th["messages_count"] == 1
    # הלקוח קיבל התראה ב-WhatsApp
    sent = c.app.state.notifier.sent
    assert sent and sent[-1]["phone"] == "972501234567" and "מה זה החיוב" in sent[-1]["text"]
    # מופיע אצל הלקוח כמשימה במסך הבית
    tasks = {t["id"] for t in c.get("/api/v1/businesses/1/home", headers=h("dana")).json()["tasks"]}
    assert "threads_client" in tasks
    r = c.post(f"/api/v1/threads/{th['id']}/messages", headers=h("dana"), json={"body": "ציוד למשרד, אעלה קבלה"})
    assert r.status_code == 200 and r.json()["waiting_on"] == "accountant"
    full = c.get(f"/api/v1/threads/{th['id']}", headers=h("acc")).json()
    assert [m["role"] for m in full["messages"]] == ["accountant", "client"]
    inbox = c.get("/api/v1/inbox", headers=h("acc")).json()
    assert [t["id"] for t in inbox["threads"]] == [th["id"]]
    assert c.post(f"/api/v1/threads/{th['id']}/close", headers=h("acc")).json()["status"] == "closed"


def test_threads_are_isolated_between_clients(c):
    th = c.post("/api/v1/businesses/1/threads", headers=h("acc"),
                json={"kind": "task", "title": "חתימה על ייפוי כוח", "body": "x"}).json()
    assert c.get(f"/api/v1/threads/{th['id']}", headers=h("moshe")).status_code == 404
    assert c.post(f"/api/v1/threads/{th['id']}/messages", headers=h("moshe"), json={"body": "x"}).status_code == 404
    assert c.get("/api/v1/businesses/1/threads", headers=h("moshe")).status_code == 404
    assert c.get("/api/v1/inbox", headers=h("moshe")).json()["threads"] == []


def test_thread_cannot_reference_document_of_another_business(c):
    d = c.post("/api/v1/businesses/2/documents/text", headers=h("adm"), json={"text": HEB_INVOICE}).json()
    r = c.post("/api/v1/businesses/1/threads", headers=h("acc"),
               json={"kind": "question", "title": "x", "body": "y", "document_id": d["id"]})
    assert r.status_code == 404


def test_client_upload_into_request_attaches_document(c):
    th = c.post("/api/v1/businesses/1/threads", headers=h("acc"),
                json={"kind": "document_request", "title": "חשבונית אופיס דיפו יולי", "body": "נא להעלות"}).json()
    r = c.post(f"/api/v1/threads/{th['id']}/upload", headers=h("dana"),
               files={"files": ("inv.pdf", make_pdf(), "application/pdf")})
    assert r.status_code == 202, r.text
    assert len(r.json()["documents"]) == 1
    full = c.get(f"/api/v1/threads/{th['id']}", headers=h("acc")).json()
    assert full["waiting_on"] == "accountant" and full["messages"][-1]["document_id"] == r.json()["documents"][0]


def test_whatsapp_or_email_document_is_attached_to_open_request(c):
    th = c.post("/api/v1/businesses/1/threads", headers=h("acc"),
                json={"kind": "document_request", "title": "קבלה", "body": "x"}).json()
    tok = c.get("/api/v1/businesses/1/connections", headers=h("adm")).json()["email"]["token"]
    payload = {"message_id": "m-1", "from": "dana@x.co", "subject": "קבלה",
               "attachments": [{"filename": "i.pdf", "content_type": "application/pdf",
                                "content_base64": base64.b64encode(make_pdf()).decode()}]}
    assert c.post(f"/api/v1/inbound/email/{tok}/json", json=payload).status_code == 202
    full = c.get(f"/api/v1/threads/{th['id']}", headers=h("acc")).json()
    assert full["waiting_on"] == "accountant" and full["messages"][-1]["document_id"]


def test_request_receipts_for_bank_charges_closes_itself(c):
    csv = "תאריך,תיאור,סכום\n16/07/2026,אופיס דיפו,-1180.00\n20/07/2026,סופר פארם,-250.00\n"
    c.post("/api/v1/businesses/1/bank/import", headers=h("adm"), files={"file": ("b.csv", csv.encode(), "text/csv")})
    r = c.post("/api/v1/businesses/1/threads/request-receipts", headers=h("acc"))
    assert r.status_code == 201, r.text
    th = r.json()
    assert len(th["refs"]["bank_tx_ids"]) == 2 and "סופר פארם" in c.get(f"/api/v1/threads/{th['id']}", headers=h("acc")).json()["messages"][0]["body"]
    # בקשה שנייה לא משכפלת חיובים שכבר התבקשו
    assert c.post("/api/v1/businesses/1/threads/request-receipts", headers=h("acc")).status_code == 204
    c.post("/api/v1/businesses/1/documents/text", headers=h("dana"), json={"text": HEB_INVOICE})
    assert c.get(f"/api/v1/threads/{th['id']}", headers=h("acc")).json()["status"] == "open"  # עוד חיוב פתוח
    c.post(f"/api/v1/bank/{max(th['refs']['bank_tx_ids'])}/ignore", headers=h("acc"))  # סופר פארם — חיוב פרטי
    c.post("/api/v1/businesses/1/documents/text", headers=h("dana"),
           json={"text": HEB_INVOICE.replace("10234", "10235").replace("15/07", "16/07")})
    assert c.get(f"/api/v1/threads/{th['id']}", headers=h("acc")).json()["status"] == "closed"


def test_client_profile_hides_internal_notes_from_client(c):
    r = c.patch("/api/v1/businesses/1/profile", headers=h("acc"),
                json={"contact_email": "dana@x.co", "tags": "מסעדנות, חודשי", "internal_notes": "לקוחה רגישה למחיר"})
    assert r.status_code == 200, r.text
    assert c.get("/api/v1/businesses/1/profile", headers=h("acc")).json()["internal_notes"] == "לקוחה רגישה למחיר"
    mine = c.get("/api/v1/businesses/1/profile", headers=h("dana")).json()
    assert "internal_notes" not in mine and mine["contact_email"] == "dana@x.co"
    assert c.patch("/api/v1/businesses/1/profile", headers=h("dana"), json={"internal_notes": "x"}).status_code == 403


def test_dashboard_shows_requests_waiting_on_accountant(c):
    th = c.post("/api/v1/businesses/1/threads", headers=h("dana"),
                json={"kind": "question", "title": "אפשר לקזז את הרכב?", "body": "קניתי רכב"}).json()
    assert th["waiting_on"] == "accountant"
    d = c.get("/api/v1/dashboard", headers=h("acc")).json()
    row = next(b for b in d["businesses"] if b["id"] == 1)
    assert row["threads"]["waiting_on_accountant"] == 1
