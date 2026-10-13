"""אופטימיזציית מס משולבת: חלוקת תקציב בין מנופים, מדרגות, תזמון הכנסה ומילואים."""
from fastapi.testclient import TestClient

from cpa_ai.advisor import optimizer
from cpa_ai.api import create_app
from cpa_ai.tax_model import estimate_burden

from .test_api import H


def test_budget_goes_to_best_marginal_levers_first():
    r = optimizer.optimize(profit=400_000, year=2026, credit_points=2.25, budget=30_000)
    alloc = {a["lever"]: a["amount"] for a in r["allocation"]}
    # בהכנסה גבוהה ניכוי (שיעור שולי ~47%) עדיף על זיכוי 35%
    assert alloc.get("keren_hishtalmut", 0) > 0 and alloc.get("pension_deduction", 0) > 0
    assert sum(alloc.values()) <= 30_000
    assert alloc.get("pension_credit", 0) <= 30_000 - alloc["keren_hishtalmut"] - alloc["pension_deduction"]
    # החיסכון שמדווח הוא בדיוק ההפרש במודל הנטל — לא הערכה גסה
    before = estimate_burden(400_000, 2026, 2.25)["total_burden"]
    assert abs(r["burden_before"] - before) < 0.01
    assert abs(r["total_saving"] - (r["burden_before"] - r["burden_after"])) < 0.01
    assert r["total_saving"] > 0


def test_caps_are_respected_and_leftover_reported():
    r = optimizer.optimize(profit=400_000, year=2026, credit_points=2.25, budget=1_000_000)
    alloc = {a["lever"]: a["amount"] for a in r["allocation"]}
    assert alloc["keren_hishtalmut"] <= 13_203 and alloc["pension_deduction"] <= 25_608
    assert r["unused_budget"] > 0


def test_low_income_prefers_credit_channel():
    r = optimizer.optimize(profit=90_000, year=2026, credit_points=2.25, budget=4_000)
    first = r["allocation"][0]
    assert first["lever"] == "pension_credit"  # שיעור שולי נמוך מ-35%


def test_donations_are_only_planned_ones():
    r = optimizer.optimize(profit=300_000, year=2026, credit_points=2.25, budget=50_000)
    assert "donation" not in {a["lever"] for a in r["allocation"]}
    r = optimizer.optimize(profit=300_000, year=2026, credit_points=2.25, budget=50_000, planned_donations=5_000)
    assert {a["lever"]: a["amount"] for a in r["allocation"]}["donation"] == 5_000


def test_bracket_position():
    b = optimizer.bracket_position(taxable=300_000, year=2026)
    assert b["marginal_rate"] > 0.3 and b["to_lower_bracket"] > 0
    assert b["to_next_bracket"] is None or b["to_next_bracket"] > 0


def test_income_timing_only_when_next_year_is_lower():
    up = optimizer.income_timing(this_year_profit=200_000, next_year_profit=400_000, year=2026, credit_points=2.25)
    assert up["best_shift"] == 0
    down = optimizer.income_timing(this_year_profit=600_000, next_year_profit=100_000, year=2026, credit_points=2.25)
    assert down["best_shift"] > 0 and down["saving"] > 0
    assert "חשבונית" in down["legal_note"]


def test_reserve_duty_base_update():
    r = optimizer.reserve_duty(planned_days=30, ni_monthly_base=8_000, actual_monthly_income=20_000)
    assert r["daily_now"] == round(max(8_000 / 30, 328.76), 2)
    assert r["daily_if_updated"] == round(20_000 / 30, 2)
    assert r["extra_compensation"] == round((r["daily_if_updated"] - r["daily_now"]) * 30, 2)
    assert r["verified"] is False and "אמיתית" in r["condition"]
    capped = optimizer.reserve_duty(planned_days=10, ni_monthly_base=60_000, actual_monthly_income=90_000)
    assert capped["daily_now"] == 1730.33 and capped["extra_compensation"] == 0
    none = optimizer.reserve_duty(planned_days=10, ni_monthly_base=20_000, actual_monthly_income=15_000)
    assert none["extra_compensation"] == 0


def test_optimize_endpoint():
    c = TestClient(create_app("sqlite://", api_key="k"))
    c.post("/api/v1/businesses", headers=H, json={"name": "ע", "vat_id": "037544129", "owner_name": "x"})
    c.post("/api/v1/businesses/1/income", headers=H, json={"doc_date": "2026-03-10", "pre_vat_amount": 300_000})
    r = c.post("/api/v1/businesses/1/advisor/optimize", headers=H, json={
        "year": 2026, "budget": 20_000, "projected_profit": 350_000, "next_year_profit": 150_000,
        "reserve": {"planned_days": 20, "ni_monthly_base": 10_000, "actual_monthly_income": 25_000}})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["plan"]["total_saving"] > 0 and p["timing"]["best_shift"] >= 0
    assert p["reserve_duty"]["extra_compensation"] > 0
    assert p["disclaimer"] and p["bracket"]["marginal_rate"] > 0
    assert any(a["action"] == "advisor_optimize" for a in c.get("/api/v1/audit", headers=H).json())
