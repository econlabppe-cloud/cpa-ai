"""בדיקות ביקורת סבב 3 — מחלץ טקסט ובטיחות קליטה."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from cpa_ai.api import create_app
from cpa_ai.extraction import heuristic
from cpa_ai.rules.engine import evaluate_expense


def test_space_thousands_separator_is_not_truncated():
    e = heuristic.extract("ספק\nח.פ 513456780\nחשבונית מס מס' 5\n10/07/2026\nסה\"כ לתשלום: 12 500.00\n")
    assert e.total_amount == 12_500.0


def test_tax_invoice_without_vat_breakdown_cannot_auto_confirm():
    e = heuristic.extract("ספק\nח.פ 513456780\nחשבונית מס מס' 5\n10/07/2026\nסה\"כ לתשלום: 1,180.00\n")
    assert e.pre_vat_amount is None and e.vat_amount is None
    d = evaluate_expense(e)
    assert d.needs_human_review is True


def test_english_labels_extracted():
    t = ("Office Depot\nVAT ID 513456780\nTax Invoice No. 99\n15/07/2026\n"
         "Subtotal 1,000.00\nVAT 18% 180.00\nTotal 1,180.00\n")
    e = heuristic.extract(t)
    assert (e.pre_vat_amount, e.vat_amount, e.total_amount) == (1000.0, 180.0, 1180.0)
    assert e.vendor_id == "513456780"
    assert e.doc_date == date(2026, 7, 15)


def test_full_vat_breakdown_keeps_normal_confidence():
    t = ("אופיס דיפו בע\"מ\nח.פ 513456780\nחשבונית מס מס' 10234\nתאריך: 15/07/2026\n"
         "סה\"כ לפני מע\"מ: 1,000.00\nמע\"מ 18%: 180.00\nסה\"כ לתשלום: 1,180.00\n")
    e = heuristic.extract(t)
    assert e.extraction_confidence >= 0.85


def test_new_document_does_not_enter_an_approved_period(client_approved_period):
    """מסמך מאוחר לא משנה דוח מאושר: הוא נקלט, משויך לתקופה הפתוחה הבאה ומחכה לבדיקה."""
    client, biz = client_approved_period
    H = {"X-API-Key": "k", "X-Actor": "t"}
    r = client.post(f"/api/v1/businesses/{biz}/income", headers=H,
                    json={"doc_date": "2026-03-20", "pre_vat_amount": 500})
    assert r.status_code == 201 and r.json()["period_date"] == "2026-05-01"
    assert r.json()["status"] == "pending_review"
    r = client.post(f"/api/v1/businesses/{biz}/documents/text", headers=H, json={"text": (
        "ספק\nח.פ 513456780\nחשבונית מס מס' 9\n20/03/2026\nלפני מע\"מ 100.00\nמע\"מ 18% 18.00\nסה\"כ לתשלום 118.00\n")})
    assert r.status_code == 201 and r.json()["period_date"] == "2026-05-01"


@pytest.fixture
def client_approved_period():
    c = TestClient(create_app("sqlite://", api_key="k"))
    H = {"X-API-Key": "k", "X-Actor": "t"}
    biz = c.post("/api/v1/businesses", headers=H,
                 json={"name": "א", "vat_id": "037544129", "owner_name": "ב"}).json()["id"]
    c.post(f"/api/v1/businesses/{biz}/income", headers=H, json={"doc_date": "2026-03-10", "pre_vat_amount": 100})
    rep = c.post(f"/api/v1/businesses/{biz}/reports", headers=H,
                 params={"kind": "vat_period", "year": 2026, "month": 3}).json()
    ok = c.post(f"/api/v1/reports/{rep['id']}/approve", headers=H,
                json={"approver_name": "ר", "license_type": "cpa", "license_number": "1234", "attest": True})
    assert ok.status_code == 200
    return c, biz
