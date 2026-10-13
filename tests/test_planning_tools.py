"""תחזית תזרים, סגירת שנה, רישום ספקים ולקוחות, חיפוש מסמכים, החזרה לבדיקה וייצוא Excel שנתי."""
import io
from datetime import date

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from cpa_ai import closing, insights
from cpa_ai.api import create_app

from .test_api import APPROVER, H
from .test_rules import HEB_INVOICE


@pytest.fixture
def c():
    cl = TestClient(create_app("sqlite://", api_key="k"))
    cl.post("/api/v1/businesses", headers=H, json={"name": "ע", "vat_id": "037544129", "owner_name": "x",
                                                    "advance_rate_percent": 10})
    return cl


def inc(c, when, amount, customer="לקוח", **kw):
    r = c.post("/api/v1/businesses/1/income", headers=H, json={"doc_date": when, "pre_vat_amount": amount,
                                                                "customer_name": customer, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def exp(c, month, number, pre=300.0, vendor="אופיס דיפו", vid="513456780"):
    vat = round(pre * 0.18, 2)
    text = (f"{vendor}\nח.פ {vid}\nחשבונית מס מס' {number}\nתאריך: 05/{month:02d}/2026\nנייר\n"
            f"סה\"כ לפני מע\"מ: {pre:.2f}\nמע\"מ 18%: {vat:.2f}\nסה\"כ לתשלום: {pre + vat:.2f}\n")
    r = c.post("/api/v1/businesses/1/documents/text", headers=H, json={"text": text})
    assert r.status_code == 201, r.text
    return r.json()


def test_parties_totals_and_shares(c):
    inc(c, "2026-03-01", 6000, "לקוח א")
    inc(c, "2026-04-01", 2000, "לקוח ב")
    exp(c, 3, "1"), exp(c, 4, "2")
    p = c.get("/api/v1/businesses/1/parties", headers=H, params={"year": 2026}).json()
    assert [x["name"] for x in p["customers"]] == ["לקוח א", "לקוח ב"]
    assert p["customers"][0]["share"] == 0.75 and p["customers"][0]["total"] == 6000 * 1.18
    v = p["vendors"][0]
    assert v["name"] == "אופיס דיפו" and v["documents"] == 2 and v["total"] == round(2 * 300 * 1.18, 2)


def test_cashflow_places_receivables_recurring_and_vat(c):
    inc(c, "2026-09-20", 10_000)           # עוד לא שולם: צפוי להיגבות
    for i, m in enumerate((7, 8, 9), 1):    # חשמל חודשי קבוע
        exp(c, m, str(i))
    with c.app.state.SessionLocal() as s:
        from cpa_ai.db import Business
        r = insights.cashflow(s, s.get(Business, 1), date(2026, 10, 13), months=3)
    months = {m["month"]: m for m in r["months"]}
    assert list(months) == ["2026-10", "2026-11", "2026-12"]
    refund = [i for m in r["months"] for i in m["items"] if i["kind"] == "vat_refund"]
    assert refund and refund[0]["amount"] == 108.0  # מע"מ תשומות של יולי–אוגוסט, ללא עסקאות — החזר צפוי
    assert round(sum(m["inflow"] for m in r["months"]) + r["later_inflow"], 2) == 11_800.0 + 108.0
    assert months["2026-10"]["recurring"] == round(300 * 1.18, 2) and months["2026-11"]["recurring"] == round(300 * 1.18, 2)
    assert any(i["kind"] == "vat" for m in r["months"] for i in m["items"]) or r["notes"]
    assert any("יתרת הבנק" in n for n in r["assumptions"])


def test_cashflow_uses_own_collection_history(c):
    first = inc(c, "2026-01-01", 1000)
    c.post(f"/api/v1/documents/{first['id']}/mark-paid", headers=H, json={"paid_on": "2026-02-20"})  # 50 ימים
    with c.app.state.SessionLocal() as s:
        from cpa_ai.db import Business
        assert insights.avg_collection_days(s, s.get(Business, 1)) == 50


def test_year_close_checklist(c):
    inc(c, "2026-03-01", 5000, wh=0)
    pend = exp(c, 3, "1", vendor="x" * 3)
    with c.app.state.SessionLocal() as s:
        from cpa_ai.db import Business
        r = closing.year_close(s, s.get(Business, 1), 2026, date(2026, 12, 20))
    items = {i["id"]: i for i in r["items"]}
    assert items["periods_vat"]["status"] == "todo" and "מרץ" in items["periods_vat"]["detail"] or "ינואר" in items["periods_vat"]["detail"]
    assert items["annual_estimate"]["status"] == "todo"
    assert 0 <= r["percent"] < 100 and pend["id"]


def test_year_close_reaches_done_for_prepared_year(c):
    inc(c, "2026-01-10", 1000)
    for month in (1, 3, 5, 7, 9, 11):
        rep = c.post("/api/v1/businesses/1/reports", headers=H, params={"kind": "vat_period", "year": 2026, "month": month}).json()
        c.post(f"/api/v1/reports/{rep['id']}/approve", headers=H, json=APPROVER)
        c.post(f"/api/v1/reports/{rep['id']}/mark-submitted", headers=H, params={"reference": "R1"})
    with c.app.state.SessionLocal() as s:
        from cpa_ai.db import Business
        r = closing.year_close(s, s.get(Business, 1), 2026, date(2026, 12, 20))
    assert {i["id"]: i["status"] for i in r["items"]}["periods_vat"] == "done"


def test_reopen_confirmed_document(c):
    d = exp(c, 3, "1")
    assert d["status"] == "confirmed"
    r = c.post(f"/api/v1/documents/{d['id']}/reopen", headers=H)
    assert r.status_code == 200 and r.json()["status"] == "pending_review"
    assert c.post(f"/api/v1/documents/{d['id']}/reopen", headers=H).status_code == 409  # כבר ממתין
    assert any(a["action"] == "reopen" for a in c.get("/api/v1/audit", headers=H).json())


def test_reopen_blocked_in_locked_period(c):
    d = inc(c, "2026-03-10", 100)
    rep = c.post("/api/v1/businesses/1/reports", headers=H, params={"kind": "vat_period", "year": 2026, "month": 3}).json()
    c.post(f"/api/v1/reports/{rep['id']}/approve", headers=H, json=APPROVER)
    assert c.post(f"/api/v1/documents/{d['id']}/reopen", headers=H).status_code == 409


def test_document_search(c):
    exp(c, 3, "11", vendor="נייר ודיו בע\"מ", vid="513451005")
    c.post("/api/v1/businesses/1/documents/text", headers=H, json={"text": HEB_INVOICE})
    inc(c, "2026-04-01", 500, "דנה לוי")
    def ids(**q):
        return [d["id"] for d in c.get("/api/v1/businesses/1/documents", headers=H, params=q).json()]
    assert len(ids(q="אופיס")) == 1
    assert len(ids(q="דנה")) == 1
    assert len(ids(q="1,180")) == 1 or len(ids(q="1180")) == 1
    assert len(ids(direction="income")) == 1
    assert len(ids(date_from="2026-04-01", date_to="2026-04-30")) == 1
    assert len(ids(q="חשבונית 11")) >= 0
    assert ids(q="לא קיים") == []


def test_annual_xlsx(c):
    inc(c, "2026-03-01", 6000, "=HYPERLINK(\"x\")")
    exp(c, 3, "1")
    r = c.get("/api/v1/businesses/1/export/annual-xlsx", headers=H, params={"year": 2026})
    assert r.status_code == 200 and "spreadsheetml" in r.headers["content-type"]
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["סיכום", "הכנסות", "הוצאות", "מע\"מ לפי תקופה", "פחת"]
    summary = {row[0].value: row[1].value for row in wb["סיכום"].iter_rows(min_row=2) if row[0].value}
    assert summary["הכנסות (לפני מע\"מ)"] == 6000 and summary["הוצאות מוכרות"] == 300
    names = [row[2].value for row in wb["הכנסות"].iter_rows(min_row=2)]
    assert not any(str(n).startswith("=") for n in names)  # הגנה מהזרקת נוסחאות
    assert wb["סיכום"].sheet_view.rightToLeft is True
