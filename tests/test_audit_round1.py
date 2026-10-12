"""בדיקות ביקורת סבב 1 — מתארות התנהגות נכונה; חלקן נכשלות לפני התיקון."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cpa_ai.api import create_app
from cpa_ai.rules import data

SAMPLES = Path(__file__).parent.parent / "samples"
H = {"X-API-Key": "k", "X-Actor": "tester"}
APPROVER = {"approver_name": "רו\"ח", "license_type": "cpa", "license_number": "12345", "attest": True}


@pytest.fixture
def client():
    return TestClient(create_app("sqlite://", api_key="k"))


@pytest.fixture
def biz(client):
    r = client.post("/api/v1/businesses", headers=H, json={"name": "א", "vat_id": "037544129", "owner_name": "ב"})
    return r.json()["id"]


def test_rejected_document_cannot_be_confirmed(client, biz):
    d = client.post(f"/api/v1/businesses/{biz}/income", headers=H,
                    json={"doc_date": "2026-03-10", "pre_vat_amount": 100}).json()
    client.post(f"/api/v1/documents/{d['id']}/review", headers=H, json={"action": "reject", "reviewer": "x"})
    r = client.post(f"/api/v1/documents/{d['id']}/review", headers=H, json={"action": "confirm", "reviewer": "x"})
    assert r.status_code == 409


def test_approved_period_is_locked(client, biz):
    d = client.post(f"/api/v1/businesses/{biz}/income", headers=H,
                    json={"doc_date": "2026-03-10", "pre_vat_amount": 100}).json()
    rep = client.post(f"/api/v1/businesses/{biz}/reports", headers=H,
                      params={"kind": "vat_period", "year": 2026, "month": 3}).json()
    assert client.post(f"/api/v1/reports/{rep['id']}/approve", headers=H, json=APPROVER).status_code == 200
    # אחרי אישור אסור לשנות מסמך שנכנס לתקופה המאושרת
    r = client.post(f"/api/v1/documents/{d['id']}/review", headers=H, json={"action": "reject", "reviewer": "x"})
    assert r.status_code == 409


def test_unified_file_refuses_lossy_encoding(client, biz):
    client.post(f"/api/v1/businesses/{biz}/income", headers=H,
                json={"doc_date": "2026-03-10", "pre_vat_amount": 100, "customer_name": "Café 😀 לקוח"})
    r = client.get(f"/api/v1/businesses/{biz}/unified-file", headers=H, params={"year": 2026})
    assert r.status_code == 422
    assert "קידוד" in r.json()["detail"]


def test_document_list_is_paginated(client, biz):
    for i in range(5):
        client.post(f"/api/v1/businesses/{biz}/income", headers=H,
                    json={"doc_date": "2026-03-10", "pre_vat_amount": 100 + i})
    page = client.get(f"/api/v1/businesses/{biz}/documents", headers=H, params={"limit": 2, "offset": 1}).json()
    assert len(page) == 2


def test_actor_comes_from_key_when_user_map_configured():
    app = create_app("sqlite://", api_key="k", users={"k": "יהודה"})
    c = TestClient(app)
    from urllib.parse import quote
    c.post("/api/v1/businesses", headers={"X-API-Key": "k", "X-Actor": quote("מישהו אחר")},
           json={"name": "א", "vat_id": "037544129", "owner_name": "ב"})
    log = c.get("/api/v1/audit", headers={"X-API-Key": "k"}).json()
    assert log, "היומן צריך להכיל את פעולת היצירה"
    assert {a["actor"] for a in log} == {"יהודה"}


def test_app_refuses_to_start_without_key(monkeypatch):
    monkeypatch.delenv("CPA_AI_API_KEY", raising=False)
    monkeypatch.delenv("CPA_AI_ALLOW_INSECURE", raising=False)
    with pytest.raises(RuntimeError):
        create_app("sqlite://")


def test_tax_table_not_marked_verified():
    # מדרגות שנלקחו ממקורות מדווחים לשנת 2026 אינן מאומתות — אסור לסמן אותן כמאומתות
    assert data.TAX_YEARS[2025].source.verified is False


def test_allocation_thresholds_not_marked_verified():
    # הסף נלקח ממקורות משניים סותרים (מעל/ומעלה, תאריכי מדרגות)
    assert data.ALLOCATION_SOURCE.verified is False
