import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cpa_ai.api import create_app

from .test_rules import HEB_INVOICE

H = {"X-API-Key": "k", "X-Actor": "tester"}
SAMPLES = Path(__file__).parent.parent / "samples"
APPROVER = {"approver_name": "רו\"ח ישראלה", "license_type": "cpa", "license_number": "12345", "attest": True}


@pytest.fixture
def client():
    return TestClient(create_app("sqlite://", api_key="k"))


@pytest.fixture
def biz(client):
    r = client.post("/api/v1/businesses", headers=H, json={
        "name": "סטודיו לדוגמה", "vat_id": "037544129", "owner_name": "דנה", "advance_rate_percent": 5})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_auth_required(client):
    assert client.get("/api/v1/businesses").status_code == 401
    assert client.get("/api/v1/health").status_code == 200


def test_invalid_business_id_rejected(client):
    r = client.post("/api/v1/businesses", headers=H, json={"name": "x", "vat_id": "123456789", "owner_name": "y"})
    assert r.status_code == 422


def test_full_flow_vat_report(client, biz):
    # הוצאה נקייה — מאושרת אוטומטית
    r = client.post(f"/api/v1/businesses/{biz}/documents/text", headers=H, json={"text": HEB_INVOICE})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "confirmed"

    # הוצאת דלק — כלל לא מאומת, ממתינה לבדיקה
    # ספק אחר = מספר עוסק ומספר מסמך אחרים (אחרת זו כפילות, וזה נבדק ב-test_ingest)
    fuel = (HEB_INVOICE.replace("אופיס דיפו", "סונול").replace("נייר A4 - 10 חבילות", "דלק 95")
            .replace("טונר HP", "").replace("513456780", "513451005").replace("10234", "20456"))
    fuel_doc = client.post(f"/api/v1/businesses/{biz}/documents/text", headers=H, json={"text": fuel}).json()
    assert fuel_doc["status"] == "pending_review"

    # הכנסה
    r = client.post(f"/api/v1/businesses/{biz}/income", headers=H, json={
        "doc_date": "2026-08-03", "pre_vat_amount": 4000, "customer_name": "לקוח", "withholding_tax": 50})
    assert r.status_code == 201 and r.json()["decision"]["vat_amount"] == 720.0

    rep = client.post(f"/api/v1/businesses/{biz}/reports", headers=H,
                      params={"kind": "vat_period", "year": 2026, "month": 7}).json()
    assert rep["period_start"] == "2026-07-01" and rep["period_end"] == "2026-08-31"
    assert rep["payload"]["pending_document_ids"] == [fuel_doc["id"]]
    # אי אפשר לאשר עם מסמכים ממתינים
    assert client.post(f"/api/v1/reports/{rep['id']}/approve", headers=H, json=APPROVER).status_code == 409

    # בודק מאשר את הדלק
    r = client.post(f"/api/v1/documents/{fuel_doc['id']}/review", headers=H,
                    json={"action": "confirm", "reviewer": "רו\"ח ישראלה"})
    assert r.status_code == 200 and r.json()["status"] == "confirmed"

    rep = client.post(f"/api/v1/businesses/{biz}/reports", headers=H,
                      params={"kind": "vat_period", "year": 2026, "month": 8}).json()
    p = rep["payload"]
    assert p["sales"]["output_vat"] == 720.0
    assert p["inputs"]["other_vat"] == 180.0 + 120.0
    assert p["vat_payable"] == 720.0 - 300.0

    # שידור לפני אישור — חסום
    assert client.post(f"/api/v1/reports/{rep['id']}/transmit", headers=H).status_code == 409
    # אישור בלי הצהרה — חסום
    assert client.post(f"/api/v1/reports/{rep['id']}/approve", headers=H,
                       json={**APPROVER, "attest": False}).status_code == 409

    ok = client.post(f"/api/v1/reports/{rep['id']}/approve", headers=H, json=APPROVER)
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    # שידור אחרי אישור — לא ממומש במכוון
    assert client.post(f"/api/v1/reports/{rep['id']}/transmit", headers=H).status_code == 501

    actions = {a["action"] for a in client.get("/api/v1/audit", headers=H).json()}
    assert {"ingest", "review_confirm", "prepare", "approve", "transmit_attempt"} <= actions


def test_stale_report_cannot_be_approved(client, biz):
    client.post(f"/api/v1/businesses/{biz}/income", headers=H, json={"doc_date": "2026-01-10", "pre_vat_amount": 1000})
    rep = client.post(f"/api/v1/businesses/{biz}/reports", headers=H,
                      params={"kind": "income_tax_advance", "year": 2026, "month": 1}).json()
    assert rep["payload"]["gross_advance"] == 50.0
    client.post(f"/api/v1/businesses/{biz}/income", headers=H, json={"doc_date": "2026-02-10", "pre_vat_amount": 1000})
    r = client.post(f"/api/v1/reports/{rep['id']}/approve", headers=H, json=APPROVER)
    assert r.status_code == 409
    assert client.get(f"/api/v1/reports/{rep['id']}", headers=H).json()["status"] == "superseded"


def test_openai_requires_consent(client, biz):
    r = client.post(f"/api/v1/businesses/{biz}/documents/text", headers=H,
                    json={"text": HEB_INVOICE, "extractor": "openai"})
    assert r.status_code == 403


def test_review_blocks_unfixed_errors_and_accepts_corrections(client, biz):
    bad = HEB_INVOICE.replace("513456780", "513456789")
    d = client.post(f"/api/v1/businesses/{biz}/documents/text", headers=H, json={"text": bad}).json()
    assert d["status"] == "pending_review"
    r = client.post(f"/api/v1/documents/{d['id']}/review", headers=H, json={"action": "confirm", "reviewer": "x"})
    assert r.status_code == 409
    fixed = {**d["extracted"], "vendor_id": "513456780"}
    r = client.post(f"/api/v1/documents/{d['id']}/review", headers=H,
                    json={"action": "confirm", "reviewer": "x", "corrections": fixed})
    assert r.status_code == 200 and r.json()["decision"]["deductible_vat"] == 180.0


def test_annual_estimate_and_unified_file(client, biz):
    client.post(f"/api/v1/businesses/{biz}/documents/text", headers=H, json={"text": HEB_INVOICE})
    client.post(f"/api/v1/businesses/{biz}/income", headers=H, json={"doc_date": "2026-03-10", "pre_vat_amount": 120_000})
    rep = client.post(f"/api/v1/businesses/{biz}/reports", headers=H,
                      params={"kind": "annual_estimate", "year": 2026, "credit_points": 2.25}).json()
    p = rep["payload"]
    assert p["business_profit"] == 119_000.0
    assert any("לא אומתה" in w for w in p["warnings"])

    r = client.get(f"/api/v1/businesses/{biz}/unified-file", headers=H, params={"year": 2026})
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    lines = z.read("BKMVDATA.TXT").decode("iso-8859-8").strip().split("\r\n")
    assert lines[0].startswith("A100") and lines[-1].startswith("Z900")
    assert sum(ln.startswith("C100") for ln in lines) == 2


def test_privacy_export_and_purge(client, biz):
    client.post(f"/api/v1/businesses/{biz}/documents/text", headers=H, json={"text": HEB_INVOICE})
    exp = client.get(f"/api/v1/businesses/{biz}/privacy/export", headers=H).json()
    assert exp["documents"][0]["raw_text"]
    assert client.post(f"/api/v1/businesses/{biz}/privacy/purge-raw", headers=H).json()["purged_documents"] == 1
    exp = client.get(f"/api/v1/businesses/{biz}/privacy/export", headers=H).json()
    assert exp["documents"][0]["raw_text"] is None
    assert exp["documents"][0]["decision"]  # רשומת החשבונאות נשמרת


def test_missing_allocation_can_be_confirmed_without_vat_deduction(client, biz):
    text = (SAMPLES / "fuel_invoice_no_allocation.txt").read_text(encoding="utf-8")
    d = client.post(f"/api/v1/businesses/{biz}/documents/text", headers=H, json={"text": text}).json()
    assert d["status"] == "pending_review"
    r = client.post(f"/api/v1/documents/{d['id']}/review", headers=H, json={"action": "confirm", "reviewer": "x"})
    assert r.status_code == 200
    assert r.json()["decision"]["deductible_vat"] == 0.0


def test_advisor_pension_and_vat_blocked(client, biz):
    client.post(f"/api/v1/businesses/{biz}/income", headers=H, json={"doc_date": "2026-03-10", "pre_vat_amount": 200_000})
    client.post(f"/api/v1/businesses/{biz}/documents/text", headers=H,
                json={"text": (SAMPLES / "fuel_invoice_no_allocation.txt").read_text(encoding="utf-8")})
    r = client.get(f"/api/v1/businesses/{biz}/advisor", headers=H, params={"year": 2026})
    assert r.status_code == 200, r.text
    p = r.json()
    by_id = {s["id"]: s for s in p["strategies"]}
    assert by_id["pension_self_employed"]["status"] == "needs_verification"
    assert by_id["pension_self_employed"]["details"]["room_deduction"] > 0
    assert by_id["vat_blocked_allocation"]["details"]["total_vat_blocked"] == 1080.0
    assert "סעיף 86" in " ".join(p["not_offered"])
    assert p["disclaimer"]
    assert any(a["action"] == "advisor_run" for a in client.get("/api/v1/audit", headers=H).json())


def test_pension_fills_better_channel_first_and_respects_caps():
    from cpa_ai.advisor.planner import pension_plan
    high = pension_plan(profit=300_000, marginal=0.47, existing=0)
    assert high["better_channel"] == "deduction"
    assert high["room_deduction"] == 25_608 and high["room_credit"] == 12_804
    low = pension_plan(profit=50_000, marginal=0.20, existing=0)
    assert low["better_channel"] == "credit"
    assert low["base_income"] == 50_000
    used = pension_plan(profit=300_000, marginal=0.47, existing=30_000)
    # 30,000 כבר הופקדו: 25,608 מלאים את הניכוי (ערך גבוה), השאר 4,392 מכסה מתוך הזיכוי
    assert used["room_deduction"] == 0
    assert used["room_credit"] == 12_804 - 4_392


def test_corporate_comparison_is_partial_and_lists_exclusions():
    from cpa_ai.advisor.planner import corporate_comparison
    c = corporate_comparison(profit=400_000, year=2025, credit_points=2.25, material_holder=False)
    assert c["company_full_distribution"]["corp_tax"] == 92_000
    assert any("ביטוח לאומי" in x for x in c["excluded"])  # ביטוח לאומי של הבעלים בחברה אינו מחושב
    assert c["status"] == "needs_verification"
    assert c["basis"][0]["verified"] is False


def test_advance_reduction_is_cash_not_tax_saving():
    from cpa_ai.advisor.planner import advance_reduction
    a = advance_reduction(turnover=300_000, advance_rate_pct=10, estimated_tax=5_000)
    assert a["estimated_cash_overpaid"] == 25_000
    assert a["estimated_saving"] == 0.0
    assert advance_reduction(300_000, 0, 5_000) is None
