"""מסמך שמגיע אחרי שהתקופה שלו אושרה: לא נדחה (במיוחד בקליטה אוטומטית) אלא משויך לתקופה הפתוחה הבאה."""
import pytest
from fastapi.testclient import TestClient

from cpa_ai.api import create_app
from cpa_ai.integrations import email_imap

from .test_api import APPROVER, H
from .test_email import FakeImap, email_bytes
from .test_rules import HEB_INVOICE


@pytest.fixture
def c():
    return TestClient(create_app("sqlite://", api_key="k"))


@pytest.fixture
def bid(c):
    return c.post("/api/v1/businesses", headers=H, json={
        "name": "ע", "vat_id": "037544129", "owner_name": "x", "advance_rate_percent": 5}).json()["id"]


def _approve(c, bid, kind, month):
    rep = c.post(f"/api/v1/businesses/{bid}/reports", headers=H,
                 params={"kind": kind, "year": 2026, "month": month}).json()
    r = c.post(f"/api/v1/reports/{rep['id']}/approve", headers=H, json=APPROVER)
    assert r.status_code == 200, r.text
    return rep


def codes(d):
    return {f["code"] for f in d["decision"]["findings"]}


def test_late_expense_goes_to_next_open_period(c, bid):
    _approve(c, bid, "vat_period", 7)  # יולי-אוגוסט נעולים
    r = c.post(f"/api/v1/businesses/{bid}/documents/text", headers=H, json={"text": HEB_INVOICE})  # 15/07
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["status"] == "pending_review"
    assert d["period_date"] == "2026-09-01"
    assert "LATE_DOCUMENT" in codes(d)
    assert d["decision"]["deductible_vat"] == 180.0  # בתוך 6 חודשים — מותר לנכות בתקופה מאוחרת
    c.post(f"/api/v1/documents/{d['id']}/review", headers=H, json={"action": "confirm", "reviewer": "x"})
    sep = c.post(f"/api/v1/businesses/{bid}/reports", headers=H,
                 params={"kind": "vat_period", "year": 2026, "month": 9}).json()["payload"]
    assert d["id"] in sep["included_document_ids"]
    assert sep["inputs"]["other_vat"] == 180.0
    # הדוח השנתי לפי תאריך המסמך — ההוצאה שייכת ל-2026 בכל מקרה
    ann = c.post(f"/api/v1/businesses/{bid}/reports", headers=H,
                 params={"kind": "annual_estimate", "year": 2026}).json()["payload"]
    assert d["id"] in ann["included_document_ids"]


def test_input_vat_older_than_six_months_is_not_deductible(c, bid):
    old = HEB_INVOICE.replace("15/07/2026", "15/01/2026")
    _approve(c, bid, "vat_period", 1)
    for m in (3, 5, 7):
        _approve(c, bid, "vat_period", m)
    d = c.post(f"/api/v1/businesses/{bid}/documents/text", headers=H, json={"text": old}).json()
    assert d["period_date"] == "2026-09-01"
    assert "VAT_DEDUCTION_LATE" in codes(d)
    assert d["decision"]["deductible_vat"] == 0.0
    assert d["decision"]["recognized_expense"] == 1180.0  # המע"מ שלא נוכה הופך להוצאה


def test_late_income_is_flagged_for_corrective_report(c, bid):
    _approve(c, bid, "vat_period", 7)
    r = c.post(f"/api/v1/businesses/{bid}/income", headers=H, json={"doc_date": "2026-08-10", "pre_vat_amount": 1000})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["status"] == "pending_review" and d["period_date"] == "2026-09-01"
    assert "LATE_INCOME" in codes(d)


def test_locked_document_still_cannot_be_changed(c, bid):
    d = c.post(f"/api/v1/businesses/{bid}/income", headers=H, json={"doc_date": "2026-03-10", "pre_vat_amount": 100}).json()
    _approve(c, bid, "vat_period", 3)
    r = c.post(f"/api/v1/documents/{d['id']}/review", headers=H, json={"action": "reject", "reviewer": "x"})
    assert r.status_code == 409


def test_late_document_review_uses_its_open_period(c, bid):
    _approve(c, bid, "vat_period", 7)
    d = c.post(f"/api/v1/businesses/{bid}/documents/text", headers=H, json={"text": HEB_INVOICE}).json()
    r = c.post(f"/api/v1/documents/{d['id']}/review", headers=H, json={"action": "confirm", "reviewer": "x"})
    assert r.status_code == 200, r.text


def test_late_email_is_not_lost(c, bid):
    _approve(c, bid, "vat_period", 7)
    tok = c.get(f"/api/v1/businesses/{bid}/connections", headers=H).json()["email"]["token"]
    r = c.post(f"/api/v1/inbound/email/{tok}", content=email_bytes(), headers={"Content-Type": "message/rfc822"})
    assert r.status_code == 202, r.text
    assert len(r.json()["documents"]) == 1


def test_imap_poison_message_does_not_block_the_rest(c, bid, monkeypatch):
    tok = c.get(f"/api/v1/businesses/{bid}/connections", headers=H).json()["email"]["token"]
    imap = FakeImap({1: email_bytes(msg_id="<a@x>", to=f"inbox+{tok}@gmail.com"),
                     2: email_bytes(msg_id="<b@x>", to=f"inbox+{tok}@gmail.com")})
    real = email_imap.ingest

    def flaky(s, biz, item, actor, **kw):
        if item.source_ref == "<b@x>":
            raise RuntimeError("boom")
        return real(s, biz, item, actor, **kw)
    monkeypatch.setattr(email_imap, "ingest", flaky)
    stats = email_imap.poll_once(c.app.state.SessionLocal, email_imap.ImapConfig("h", "u", "p"), imap)
    assert stats["documents"] == 1 and stats["errors"] == 1
    assert imap.seen == {1, 2}  # ההודעה הבעייתית מסומנת כדי לא ליפול עליה שוב ושוב
    log = c.get("/api/v1/audit", headers=H).json()
    assert any(a["action"] == "inbound_failed" for a in log)
