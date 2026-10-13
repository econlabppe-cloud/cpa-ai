"""יתרת מס שנתית, גיול לקוחות, רווח והפסד חודשי, וחשבונית חוזרת שחסרה."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from cpa_ai import home, insights
from cpa_ai.api import create_app

from .test_api import APPROVER, H


@pytest.fixture
def c():
    return TestClient(create_app("sqlite://", api_key="k"))


@pytest.fixture
def bid(c):
    return c.post("/api/v1/businesses", headers=H, json={
        "name": "ע", "vat_id": "037544129", "owner_name": "x", "advance_rate_percent": 10}).json()["id"]


def income(c, bid, when, amount, **kw):
    r = c.post(f"/api/v1/businesses/{bid}/income", headers=H,
               json={"doc_date": when, "pre_vat_amount": amount, "customer_name": "לקוח", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def test_annual_estimate_shows_prepaid_and_balance(c, bid):
    income(c, bid, "2026-01-10", 100_000, withholding_tax=2_000)
    rep = c.post(f"/api/v1/businesses/{bid}/reports", headers=H,
                 params={"kind": "income_tax_advance", "year": 2026, "month": 1}).json()
    assert rep["payload"]["advance_payable"] == 8_000.0  # 10% פחות ניכוי במקור
    assert c.post(f"/api/v1/reports/{rep['id']}/approve", headers=H, json=APPROVER).status_code == 200
    a = c.post(f"/api/v1/businesses/{bid}/reports", headers=H,
               params={"kind": "annual_estimate", "year": 2026}).json()["payload"]
    pre = a["prepaid"]
    assert pre["withholding"] == 2_000.0 and pre["advances"] == 8_000.0 and pre["total"] == 10_000.0
    assert a["income_tax_balance"] == round(a["estimated_income_tax"] - 10_000.0, 2)
    assert a["income_tax_balance"] < 0  # שילם יותר מהמס המוערך — צפוי החזר
    assert any("החזר" in w for w in a["warnings"])


def test_receivables_aging_and_mark_paid(c, bid):
    old = income(c, bid, "2026-06-01", 1_000)
    new = income(c, bid, "2026-10-01", 2_000)
    r = c.get(f"/api/v1/businesses/{bid}/receivables", headers=H, params={"today": "2026-10-13"}).json()
    assert r["total_open"] == 1_180.0 + 2_360.0
    assert r["buckets"]["90+"] == 1_180.0 and r["buckets"]["0-30"] == 2_360.0
    assert r["bank_imported"] is False
    p = c.post(f"/api/v1/documents/{old['id']}/mark-paid", headers=H, json={"paid_on": "2026-07-01"})
    assert p.status_code == 200, p.text
    r = c.get(f"/api/v1/businesses/{bid}/receivables", headers=H, params={"today": "2026-10-13"}).json()
    assert [x["document_id"] for x in r["items"]] == [new["id"]]
    log = c.get("/api/v1/audit", headers=H).json()
    assert any(x["action"] == "mark_paid" for x in log)


def test_receivable_paid_by_bank_match_is_closed(c, bid):
    d = income(c, bid, "2026-09-01", 1_000)
    csv = "תאריך,תיאור,סכום\n03/09/2026,העברה מלקוח,1180.00\n"
    r = c.post(f"/api/v1/businesses/{bid}/bank/import", headers=H, files={"file": ("b.csv", csv.encode(), "text/csv")})
    assert r.json()["auto_matched"] == 1, r.text
    r = c.get(f"/api/v1/businesses/{bid}/receivables", headers=H, params={"today": "2026-10-13"}).json()
    assert d["id"] not in [x["document_id"] for x in r["items"]]


def test_monthly_pnl(c, bid):
    income(c, bid, "2026-01-10", 1_000)
    income(c, bid, "2026-03-10", 3_000)
    p = c.get(f"/api/v1/businesses/{bid}/pnl", headers=H, params={"year": 2026}).json()
    assert len(p["months"]) == 12
    assert p["months"][0]["income"] == 1_000.0 and p["months"][2]["income"] == 3_000.0
    assert p["total"]["income"] == 4_000.0


def _vendor_doc(c, bid, month, number):
    text = (f'חברת חשמל\nח.פ 520000472\nחשבונית מס מס\' {number}\nתאריך: 05/{month:02d}/2026\nחשמל\n'
            'סה"כ לפני מע"מ: 300.00\nמע"מ 18%: 54.00\nסה"כ לתשלום: 354.00\n')
    assert c.post(f"/api/v1/businesses/{bid}/documents/text", headers=H, json={"text": text}).status_code == 201


def test_missing_recurring_invoice_becomes_a_task(c, bid):
    for i, m in enumerate((6, 7, 8)):
        _vendor_doc(c, bid, m, f"5{i}")
    with c.app.state.SessionLocal() as s:
        from cpa_ai.db import Business
        biz = s.get(Business, bid)
        miss = insights.missing_recurring(s, biz, date(2026, 10, 13))
        assert [m["vendor"] for m in miss] == ["חברת חשמל"] and miss[0]["month"] == "2026-09"
        tasks = {t["id"] for t in home.build_home(s, biz, date(2026, 10, 13))["tasks"]}
        assert "missing_recurring" in tasks
        _vendor_doc(c, bid, 9, "59")
        s.expire_all()
        assert insights.missing_recurring(s, biz, date(2026, 10, 13)) == []
