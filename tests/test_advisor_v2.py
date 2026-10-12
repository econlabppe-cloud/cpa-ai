from datetime import date

import pytest
from fastapi.testclient import TestClient

from cpa_ai.advisor import advise
from cpa_ai.api import create_app
from cpa_ai.db import Business

H = {"X-API-Key": "k", "X-Actor": "t"}


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_AI_DATA_DIR", str(tmp_path / "data"))
    client = TestClient(create_app("sqlite://", api_key="k"))
    bid = client.post("/api/v1/businesses", headers=H, json={"name": "א", "vat_id": "037544129", "owner_name": "ב"}).json()["id"]
    return client, bid


def income(client, bid, amount, date_str="2026-03-10"):
    assert client.post(f"/api/v1/businesses/{bid}/income", headers=H,
                       json={"doc_date": date_str, "pre_vat_amount": amount}).status_code == 201


def expense(client, bid, *, vendor="ספק", vid="513451005", number="1", pre="1000.00", extra="", item="נייר"):
    vat = f"{float(pre) * 0.18:.2f}"
    text = (f"{vendor}\nח.פ {vid}\nחשבונית מס מס' {number}\nתאריך: 15/04/2026\n{item}\n{extra}\n"
            f'סה"כ לפני מע"מ: {pre}\nמע"מ 18%: {vat}\nסה"כ לתשלום: {float(pre) + float(vat):.2f}\n')
    r = client.post(f"/api/v1/businesses/{bid}/documents/text", headers=H, json={"text": text})
    assert r.status_code == 201, r.text
    return r.json()


def plan(client, bid, **params):
    r = client.get(f"/api/v1/businesses/{bid}/advisor", headers=H, params={"year": 2026, **params})
    assert r.status_code == 200, r.text
    return r.json()


def ids(p):
    return {s["id"] for s in p["strategies"]}


def strategy(p, sid):
    return next(s for s in p["strategies"] if s["id"] == sid)


def test_year_end_calendar_counts_days_from_the_given_date(c):
    client, bid = c
    s = client.app.state.SessionLocal()
    try:
        p = advise(s, s.get(Business, bid), 2026, 2.25, 0, 0, False, today=date(2026, 11, 20))
    finally:
        s.close()
    cal = strategy(p, "year_end_calendar")["details"]
    assert cal["days_to_year_end"] == 41
    assert any("פנסיה" in i["action"] for i in cal["items"])
    assert all(i["days_left"] >= 0 for i in cal["items"])


def test_no_year_end_calendar_for_a_past_year(c):
    client, bid = c
    s = client.app.state.SessionLocal()
    try:
        p = advise(s, s.get(Business, bid), 2025, 2.25, 0, 0, False, today=date(2026, 3, 1))
    finally:
        s.close()
    assert "year_end_calendar" not in {x["id"] for x in p["strategies"]}


def test_credit_points_review_shows_value_per_point(c):
    client, bid = c
    income(client, bid, 100_000)
    d = strategy(plan(client, bid), "credit_points_review")["details"]
    assert d["value_per_point"] == 2_904 and len(d["possible_entitlements"]) >= 5
    assert "credit_points_review" not in ids(plan(client, bid, credit_points=5))  # כבר הוזנו נקודות נוספות


def test_status_review_compares_vat_recovered_with_exempt_status(c):
    client, bid = c
    income(client, bid, 80_000)
    expense(client, bid, pre="5000.00", extra="מספר הקצאה: 123456789")
    d = strategy(plan(client, bid), "status_review")["details"]
    assert d["turnover"] == 80_000 and d["input_vat_recovered_ytd"] == pytest.approx(900, abs=1)
    assert d["ceiling"] == 122_833 and d["below_ceiling"] is True


def test_no_status_review_above_the_exempt_ceiling(c):
    client, bid = c
    income(client, bid, 300_000)
    assert "status_review" not in ids(plan(client, bid))


def test_cash_payment_strategy_appears_when_cash_documents_exist(c):
    client, bid = c
    expense(client, bid, pre="4000.00", extra="שולם במזומן")
    p = plan(client, bid)
    d = strategy(p, "cash_discipline")["details"]
    assert d["documents"] == 1 and d["limit"] == 6_000


def test_foreign_invoices_strategy(c):
    client, bid = c
    client.post(f"/api/v1/businesses/{bid}/documents/text", headers=H, json={"text":
        "Google Ireland Ltd\nTax Invoice No. 55\n10/07/2026\nGoogle Workspace\nTotal $120.00\n"})
    p = plan(client, bid)
    assert strategy(p, "foreign_invoices")["details"]["documents"] == 1


def test_loss_carryforward_note_when_business_lost_money(c):
    client, bid = c
    income(client, bid, 1_000)
    doc = expense(client, bid, pre="20000.00", extra="מספר הקצאה: 123456789")
    client.post(f"/api/v1/documents/{doc['id']}/review", headers=H,
                json={"action": "confirm", "reviewer": "x", "category_override": "professional_services"})
    assert "loss_carryforward" in ids(plan(client, bid))


def test_projected_profit_overrides_year_to_date_and_changes_the_marginal_rate(c):
    client, bid = c
    income(client, bid, 30_000)
    ytd = plan(client, bid)
    proj = plan(client, bid, projected_profit=400_000)
    assert ytd["profit_basis"]["basis"] == "ytd" and proj["profit_basis"]["basis"] == "projected"
    assert proj["marginal_rate"] > ytd["marginal_rate"]
    assert strategy(proj, "pension_self_employed")["estimated_annual_saving"] > strategy(ytd, "pension_self_employed")["estimated_annual_saving"]


def test_mid_year_ytd_profit_carries_an_annualization_warning(c):
    client, bid = c
    income(client, bid, 30_000)
    s = client.app.state.SessionLocal()
    try:
        p = advise(s, s.get(Business, bid), 2026, 2.25, 0, 0, False, today=date(2026, 6, 30))
    finally:
        s.close()
    pb = p["profit_basis"]
    assert pb["basis"] == "ytd" and pb["annualized_suggestion"] == pytest.approx(30_000 * 365 / 181, abs=1)
    assert "תחזית" in pb["warning"]


def test_corporate_comparison_now_shows_social_security_for_the_individual(c):
    client, bid = c
    income(client, bid, 300_000)
    corp = strategy(plan(client, bid), "corporate_vs_individual")["details"]
    assert corp["individual_social_security"] > 0 and "ביטוח לאומי" in " ".join(corp["excluded"])


# ── נקודת קצה what-if ──────────────────────────────────────────────────────
def test_what_if_endpoint(c):
    client, bid = c
    income(client, bid, 200_000)
    r = client.post(f"/api/v1/businesses/{bid}/advisor/what-if", headers=H, json={
        "year": 2026, "scenarios": [{"type": "pension", "amount": 11_000}, {"type": "donation", "amount": 10_000}]})
    assert r.status_code == 200, r.text
    b = r.json()
    assert len(b["scenarios"]) == 2 and b["combined"]["delta_total"] < 0
    assert b["profit_basis"]["basis"] == "ytd"


def test_what_if_with_projected_profit(c):
    client, bid = c
    r = client.post(f"/api/v1/businesses/{bid}/advisor/what-if", headers=H, json={
        "year": 2026, "projected_profit": 200_000, "scenarios": [{"type": "pension", "amount": 11_000}]})
    assert r.json()["scenarios"][0]["delta_income_tax"] == pytest.approx(-3_850, abs=1)


@pytest.mark.parametrize("scn", [{"type": "magic", "amount": 1}, {"type": "donation", "amount": -1},
                                  {"type": "home_office", "business_rooms": 5, "total_rooms": 3, "annual_costs": 1000}])
def test_what_if_rejects_invalid_scenarios_with_422(c, scn):
    client, bid = c
    r = client.post(f"/api/v1/businesses/{bid}/advisor/what-if", headers=H, json={"year": 2026, "scenarios": [scn]})
    assert r.status_code == 422


def test_what_if_limits_the_number_of_scenarios(c):
    client, bid = c
    r = client.post(f"/api/v1/businesses/{bid}/advisor/what-if", headers=H,
                    json={"year": 2026, "scenarios": [{"type": "donation", "amount": 1}] * 11})
    assert r.status_code == 422


def test_pension_action_explains_which_channel_fills_first_and_why(c):
    client, bid = c
    income(client, bid, 200_000)
    s = strategy(plan(client, bid), "pension_self_employed")
    text = " ".join(s["actions"])
    assert "קודם" in text and "35%" in text and "זיכוי" in text    # בשיעור שולי 20% הזיכוי משתלם יותר


def test_company_comparison_never_shows_a_negative_saving(c):
    client, bid = c
    income(client, bid, 200_000)
    s = strategy(plan(client, bid), "corporate_vs_individual")
    assert s["estimated_annual_saving"] >= 0
    assert any("נטו" in a for a in s["actions"])


def test_strategies_are_sorted_by_saving_with_the_informational_ones_last(c):
    client, bid = c
    income(client, bid, 200_000)
    sav = [x["estimated_annual_saving"] for x in plan(client, bid)["strategies"]]
    assert sav == sorted(sav, reverse=True)
