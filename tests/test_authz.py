"""הרשאות לפי עסק ותפקיד. לקוח רואה רק את העסק שלו; רו"ח רואה את הלקוחות שלו; מנהל רואה הכל."""
import pytest
from fastapi.testclient import TestClient

from cpa_ai.api import create_app
from .helpers import make_pdf

ADMIN = {"X-API-Key": "k-admin"}
ACC = {"X-API-Key": "k-acc"}
C1 = {"X-API-Key": "k-c1"}
C2 = {"X-API-Key": "k-c2"}
APPROVER = {"approver_name": "ר", "license_type": "cpa", "license_number": "1234", "attest": True}
USERS = {
    "k-admin": "מנהל",
    "k-acc": {"name": "רו\"ח ישראלה", "role": "accountant", "businesses": [1, 2]},
    "k-c1": {"name": "דנה", "role": "client", "businesses": [1]},
    "k-c2": {"name": "יוסי", "role": "client", "businesses": [2]},
}
TEXT = ("ספק\nח.פ 513451005\nחשבונית מס מס' {n}\nתאריך: 15/07/2026\nנייר\n"
        'סה"כ לפני מע"מ: 1000.00\nמע"מ 18%: 180.00\nסה"כ לתשלום: 1180.00\n')


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_AI_DATA_DIR", str(tmp_path / "data"))
    c = TestClient(create_app("sqlite://", users=USERS))
    b1 = c.post("/api/v1/businesses", headers=ADMIN, json={"name": "א", "vat_id": "037544129", "owner_name": "ב"}).json()["id"]
    b2 = c.post("/api/v1/businesses", headers=ADMIN, json={"name": "ג", "vat_id": "513451005", "owner_name": "ד"}).json()["id"]
    assert (b1, b2) == (1, 2)
    d2 = c.post(f"/api/v1/businesses/{b2}/documents/text", headers=ADMIN, json={"text": TEXT.format(n=2)}).json()
    r2 = c.post(f"/api/v1/businesses/{b2}/reports", headers=ADMIN, params={"kind": "vat_period", "year": 2026, "month": 7}).json()
    c.post(f"/api/v1/businesses/{b2}/bank/import", headers=ADMIN,
           files={"file": ("b.csv", "תאריך,תיאור,חובה,זכות\n02/08/2026,סונול,\"230.00\",\n".encode(), "text/csv")})
    s = c.app.state.SessionLocal()
    from cpa_ai.db import BankTransaction, VendorRule
    tx = s.query(BankTransaction).filter_by(business_id=b2).first()
    s.add(VendorRule(business_id=b2, vendor_id="513451005", category="software", created_by="x"))
    s.commit(); tx_id = tx.id; rule_id = s.query(VendorRule).first().id; s.close()
    return c, {"doc2": d2["id"], "rep2": r2["id"], "tx2": tx_id, "rule2": rule_id}


def test_client_lists_only_their_own_business(env):
    c, _ = env
    assert [b["id"] for b in c.get("/api/v1/businesses", headers=C1).json()] == [1]
    assert [b["id"] for b in c.get("/api/v1/businesses", headers=ACC).json()] == [1, 2]
    assert [b["id"] for b in c.get("/api/v1/businesses", headers=ADMIN).json()] == [1, 2]


@pytest.mark.parametrize("method,path,kw", [
    ("get", "/api/v1/businesses/2", {}),
    ("get", "/api/v1/businesses/2/documents", {}),
    ("post", "/api/v1/businesses/2/documents/text", {"json": {"text": TEXT.format(n=9)}}),
    ("get", "/api/v1/businesses/2/connections", {}),                     # כולל טוקן סודי של כתובת המייל
    ("get", "/api/v1/businesses/2/home", {}),
    ("get", "/api/v1/businesses/2/advisor?year=2026", {}),
    ("post", "/api/v1/businesses/2/advisor/what-if", {"json": {"year": 2026, "scenarios": [{"type": "donation", "amount": 1}]}}),
    ("get", "/api/v1/businesses/2/accountant-package?year=2026&month=7", {}),
    ("get", "/api/v1/businesses/2/privacy/export", {}),
    ("post", "/api/v1/businesses/2/privacy/purge-raw", {}),
    ("get", "/api/v1/businesses/2/unified-file?year=2026", {}),
    ("get", "/api/v1/businesses/2/vendor-rules", {}),
    ("get", "/api/v1/businesses/2/reports", {}),
    ("get", "/api/v1/businesses/2/bank/unmatched", {}),
    ("put", "/api/v1/businesses/2/consent?llm_consent=true", {}),
    ("post", "/api/v1/businesses/2/inbound-token/rotate", {}),
    ("put", "/api/v1/businesses/2/whatsapp-phone?phone=0501234567", {}),
])
def test_client_cannot_touch_another_business(env, method, path, kw):
    c, _ = env
    r = getattr(c, method)(path, headers=C1, **kw)
    assert r.status_code == 404, (path, r.status_code, r.text[:120])  # 404 ולא 403: לא מגלים שהעסק קיים


def test_entity_routes_are_scoped_through_their_business(env):
    c, ids = env
    for method, path, kw in [
        ("get", f"/api/v1/documents/{ids['doc2']}", {}),
        ("get", f"/api/v1/documents/{ids['doc2']}/file", {}),
        ("post", f"/api/v1/documents/{ids['doc2']}/review", {"json": {"action": "reject", "reviewer": "x"}}),
        ("get", f"/api/v1/reports/{ids['rep2']}", {}),
        ("post", f"/api/v1/reports/{ids['rep2']}/approve", {"json": APPROVER}),
        ("post", f"/api/v1/bank/{ids['tx2']}/ignore", {}),
        ("post", f"/api/v1/bank/{ids['tx2']}/match?document_id={ids['doc2']}", {}),
        ("delete", f"/api/v1/vendor-rules/{ids['rule2']}", {}),
    ]:
        r = getattr(c, method)(path, headers=C1, **kw)
        assert r.status_code == 404, (path, r.status_code)
    assert c.get(f"/api/v1/documents/{ids['doc2']}", headers=C2).status_code == 200  # ללקוח הנכון — כן


def test_own_business_is_fully_usable(env):
    c, _ = env
    r = c.post("/api/v1/businesses/1/documents/text", headers=C1, json={"text": TEXT.format(n=5)})
    assert r.status_code == 201
    assert c.get("/api/v1/businesses/1/home", headers=C1).status_code == 200


def test_only_admin_creates_businesses_and_reads_the_global_audit(env):
    c, _ = env
    body = {"name": "x", "vat_id": "123456782", "owner_name": "y"}
    assert c.post("/api/v1/businesses", headers=C1, json=body).status_code == 403
    assert c.post("/api/v1/businesses", headers=ACC, json=body).status_code == 403
    assert c.get("/api/v1/audit", headers=C1).status_code == 403
    assert c.get("/api/v1/audit/verify", headers=ACC).status_code == 403
    assert c.get("/api/v1/audit", headers=ADMIN).status_code == 200


def test_client_cannot_approve_or_mark_submitted_but_accountant_can(env):
    c, ids = env
    c.post("/api/v1/businesses/1/documents/text", headers=C1, json={"text": TEXT.format(n=7)})
    rep = c.post("/api/v1/businesses/1/reports", headers=C1, params={"kind": "vat_period", "year": 2026, "month": 7}).json()
    assert c.post(f"/api/v1/reports/{rep['id']}/approve", headers=C1, json=APPROVER).status_code == 403
    ok = c.post(f"/api/v1/reports/{rep['id']}/approve", headers=ACC, json=APPROVER)
    assert ok.status_code == 200
    assert c.post(f"/api/v1/reports/{rep['id']}/mark-submitted?reference=X", headers=C1).status_code == 403
    assert c.post(f"/api/v1/reports/{rep['id']}/mark-submitted?reference=X", headers=ACC).status_code == 200


def test_actor_is_always_derived_from_the_key_for_named_users(env):
    c, _ = env
    c.post("/api/v1/businesses/1/documents/text", headers={**C1, "X-Actor": "someone-else"}, json={"text": TEXT.format(n=11)})
    actors = {a["actor"] for a in c.get("/api/v1/audit", headers=ADMIN).json() if a["action"] == "ingest"}
    assert "דנה" in actors and "someone-else" not in actors  # השם הנטען בכותרת לא נכנס ליומן


def test_denied_attempts_are_written_to_the_audit_log(env):
    c, _ = env
    c.get("/api/v1/businesses/2/documents", headers=C1)
    denied = [a for a in c.get("/api/v1/audit", headers=ADMIN).json() if a["action"] == "access_denied"]
    assert denied and denied[0]["actor"] == "דנה" and denied[0]["details"]["business_id"] == 2


def test_invalid_or_missing_key_is_rejected(env):
    c, _ = env
    assert c.get("/api/v1/businesses").status_code == 401
    assert c.get("/api/v1/businesses", headers={"X-API-Key": "nope"}).status_code == 401


def test_inbound_endpoints_do_not_need_a_user_key(env):
    c, _ = env
    assert c.post("/api/v1/inbound/email/not-a-token", content=b"x").status_code == 404  # לא 401


def test_shared_key_mode_is_still_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_AI_DATA_DIR", str(tmp_path / "d"))
    c = TestClient(create_app("sqlite://", api_key="shared"))
    from urllib.parse import quote
    h = {"X-API-Key": "shared", "X-Actor": quote("דנה")}
    assert c.post("/api/v1/businesses", headers=h, json={"name": "א", "vat_id": "037544129", "owner_name": "ב"}).status_code == 201
    assert c.get("/api/v1/audit", headers=h).status_code == 200


def test_users_env_var_supports_objects(monkeypatch):
    import json
    monkeypatch.setenv("CPA_AI_USERS", json.dumps({"k": {"name": "ד", "role": "client", "businesses": [3]}}))
    c = TestClient(create_app("sqlite://"))
    assert c.get("/api/v1/businesses", headers={"X-API-Key": "k"}).json() == []


def test_unknown_role_in_config_fails_fast():
    with pytest.raises(ValueError):
        create_app("sqlite://", users={"k": {"name": "x", "role": "root"}})


def test_pdf_upload_still_works_for_a_scoped_client(env):
    c, _ = env
    r = c.post("/api/v1/businesses/1/documents/upload", headers=C1, files=[("files", ("i.pdf", make_pdf(), "application/pdf"))])
    assert r.status_code == 202 and len(r.json()["documents"]) == 1
