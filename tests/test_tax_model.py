"""מספרים שחושבו ידנית (ראו הערות) — כדי לתפוס טעויות בנוסחאות, לא רק בפרמטרים."""
from datetime import date

import pytest

from cpa_ai import depreciation as dep
from cpa_ai.tax_model import donation_credit, estimate_burden, social_security


def test_social_security_two_brackets():
    # 120,000/12 = 10,000: 7,703×7.7% + 2,297×18% = 593.131 + 413.46 = 1,006.591 לחודש
    ss = social_security(120_000)
    assert ss["total"] == pytest.approx(1_006.591 * 12, abs=0.5)
    assert ss["deductible_from_income"] == pytest.approx(ss["total"] * 0.52, abs=0.01)
    assert not ss["minimum_applied"] and not ss["capped"]


def test_social_security_minimum_income_applies_to_low_and_negative_profit():
    low = social_security(12_000)           # 1,000 לחודש < 3,442 → משלם על 3,442
    assert low["minimum_applied"] and low["total"] == pytest.approx(3_442 * 0.077 * 12, abs=0.5)
    assert social_security(-50_000)["total"] == low["total"]


def test_social_security_is_capped_at_the_ceiling():
    cap = social_security(1_000_000)
    assert cap["capped"] and cap["total"] == pytest.approx((7_703 * 0.077 + (51_910 - 7_703) * 0.18) * 12, abs=1)
    assert social_security(5_000_000)["total"] == cap["total"]


def test_social_security_marked_unverified():
    assert social_security(100_000)["verified"] is False


def test_burden_for_200k_profit():
    # BL ≈ 26,479; ניכוי 52% ≈ 13,769 → הכנסה חייבת ≈ 186,231
    # מס: 8,412 + 5,124 + (186,231-120,720)×20% = 26,638; פחות 2.25×2,904 = 6,534 → ≈ 20,104
    b = estimate_burden(200_000, 2026, 2.25)
    assert b["social_security"]["total"] == pytest.approx(26_479, abs=2)
    assert b["taxable_income"] == pytest.approx(186_231, abs=2)
    assert b["income_tax"] == pytest.approx(20_104, abs=3)
    assert b["total_burden"] == pytest.approx(b["income_tax"] + b["social_security"]["total"], abs=0.01)


def test_carried_loss_reduces_income_tax_at_the_marginal_rate_but_not_social_security():
    base = estimate_burden(200_000, 2026, 2.25)
    with_loss = estimate_burden(200_000, 2026, 2.25, carried_loss=50_000)
    assert base["income_tax"] - with_loss["income_tax"] == pytest.approx(50_000 * 0.20, abs=2)  # כולו במדרגת 20%
    assert with_loss["social_security"]["total"] == base["social_security"]["total"]


def test_donation_credit_rules():
    assert donation_credit(100, 200_000) == 0.0                 # מתחת ל-207
    assert donation_credit(10_000, 200_000) == 3_500.0          # 35%
    assert donation_credit(1_000_000, 200_000) == pytest.approx(200_000 * 0.30 * 0.35)  # תקרת 30% מההכנסה


def test_donations_lower_the_tax_by_the_credit():
    base = estimate_burden(200_000, 2026, 2.25)
    d = estimate_burden(200_000, 2026, 2.25, donations=10_000)
    assert base["income_tax"] - d["income_tax"] == pytest.approx(3_500, abs=1)


def test_tax_never_negative_and_zero_profit_pays_only_minimum_social_security():
    b = estimate_burden(0, 2026, 2.25)
    assert b["income_tax"] == 0.0 and b["social_security"]["minimum_applied"]


def test_extra_deduction_and_credit_are_applied():
    base = estimate_burden(200_000, 2026, 2.25)
    assert estimate_burden(200_000, 2026, 2.25, extra_deduction=20_000)["income_tax"] < base["income_tax"]
    assert base["income_tax"] - estimate_burden(200_000, 2026, 2.25, extra_credit=1_000)["income_tax"] == pytest.approx(1_000, abs=0.5)


# ── פחת ──────────────────────────────────────────────────────────────────────
def test_computer_depreciation_prorated_in_first_year_then_full_then_capped():
    # 6,000 ₪ ב-1.7.2026: 184 ימים נותרו → 6,000×33%×184/365 = 998.14; אחר כך 1,980 פעמיים; השארית 1,041.86
    cost, d0, rate = 6_000, date(2026, 7, 1), 0.33
    assert dep.depreciation_for_year(cost, d0, rate, 2025) == 0.0
    assert dep.depreciation_for_year(cost, d0, rate, 2026) == pytest.approx(998.14, abs=0.01)
    assert dep.depreciation_for_year(cost, d0, rate, 2027) == 1_980.0
    assert dep.depreciation_for_year(cost, d0, rate, 2028) == 1_980.0
    assert dep.depreciation_for_year(cost, d0, rate, 2029) == pytest.approx(1_041.86, abs=0.01)
    assert dep.depreciation_for_year(cost, d0, rate, 2030) == 0.0
    assert sum(r["amount"] for r in dep.schedule(cost, d0, rate)) == pytest.approx(cost, abs=0.02)


def test_purchase_in_december_gets_almost_no_first_year_depreciation():
    assert dep.depreciation_for_year(6_000, date(2026, 12, 30), 0.33, 2026) < 12


def test_asset_classification_from_description():
    assert dep.classify_asset("KSP", ["מחשב נייד Lenovo"]) == "computer"
    assert dep.classify_asset("איקאה", ["שולחן עבודה"]) == "furniture"
    assert dep.classify_asset("ספק", ["מקרן לחדר ישיבות"]) == "office_equipment"
    assert dep.classify_asset("ספק", ["משהו אחר"]) == "other"


# ── שילוב בהערכה השנתית ──────────────────────────────────────────────────────
from fastapi.testclient import TestClient  # noqa: E402

from cpa_ai.api import create_app  # noqa: E402

H = {"X-API-Key": "k", "X-Actor": "t"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_AI_DATA_DIR", str(tmp_path / "data"))
    c = TestClient(create_app("sqlite://", api_key="k"))
    bid = c.post("/api/v1/businesses", headers=H, json={"name": "א", "vat_id": "037544129", "owner_name": "ב"}).json()["id"]
    return c, bid


def laptop(client, bid, date_str="01/07/2026", number="900"):
    text = (f"KSP בע\"מ\nח.פ 513451005\nחשבונית מס מס' {number}\nתאריך: {date_str}\nמחשב נייד Lenovo\n"
            'מספר הקצאה: 123456789\nסה"כ לפני מע"מ: 6000.00\nמע"מ 18%: 1080.00\nסה"כ לתשלום: 7080.00\n')
    r = client.post(f"/api/v1/businesses/{bid}/documents/text", headers=H, json={"text": text})
    assert r.status_code == 201, r.text
    doc = r.json()
    if doc["status"] != "confirmed":
        client.post(f"/api/v1/documents/{doc['id']}/review", headers=H,
                    json={"action": "confirm", "reviewer": "x", "category_override": "equipment"})
    return doc


def annual(client, bid, year=2026, **params):
    return client.post(f"/api/v1/businesses/{bid}/reports", headers=H,
                       params={"kind": "annual_estimate", "year": year, **params}).json()["payload"]


def test_annual_estimate_depreciates_equipment_and_includes_social_security(client):
    c, bid = client
    c.post(f"/api/v1/businesses/{bid}/income", headers=H, json={"doc_date": "2026-03-10", "pre_vat_amount": 200_000})
    laptop(c, bid)
    p = annual(c, bid)
    assert p["depreciation"][0]["asset_class"] == "computer"
    # עלות: 6,000 + 1,080 מע"מ שנוכה... המע"מ מנוכה במלואו, לכן העלות 6,000. פחת 2026: 6,000×33%×184/365
    assert p["depreciation"][0]["depreciation"] == pytest.approx(998.14, abs=0.05)
    assert p["recognized_expenses_by_category"]["depreciation"] == pytest.approx(998.14, abs=0.05)
    assert p["business_profit"] == pytest.approx(200_000 - 998.14, abs=0.1)
    assert p["social_security"]["total"] > 20_000 and p["total_burden"] > p["estimated_income_tax"]
    assert any("שיעורים שלא אומתו" in w for w in p["warnings"])


def test_depreciation_continues_in_following_years(client):
    c, bid = client
    laptop(c, bid)
    assert annual(c, bid, 2027)["recognized_expenses_by_category"]["depreciation"] == pytest.approx(1_980.0, abs=0.01)


def test_report_inputs_are_kept_and_change_the_estimate(client):
    c, bid = client
    c.post(f"/api/v1/businesses/{bid}/income", headers=H, json={"doc_date": "2026-03-10", "pre_vat_amount": 200_000})
    base = annual(c, bid)
    p = annual(c, bid, donations=10_000, carried_loss=50_000)
    assert p["report_inputs"] == {"credit_points": 2.25, "donations": 10_000.0, "carried_loss": 50_000.0}
    assert base["estimated_income_tax"] - p["estimated_income_tax"] == pytest.approx(10_000 + 3_500, abs=5)


def test_approval_uses_the_stored_inputs_so_the_hash_still_matches(client):
    c, bid = client
    c.post(f"/api/v1/businesses/{bid}/income", headers=H, json={"doc_date": "2026-03-10", "pre_vat_amount": 100_000})
    r = c.post(f"/api/v1/businesses/{bid}/reports", headers=H,
               params={"kind": "annual_estimate", "year": 2026, "donations": 1_000, "carried_loss": 5_000}).json()
    ok = c.post(f"/api/v1/reports/{r['id']}/approve", headers=H,
                json={"approver_name": "ר", "license_type": "cpa", "license_number": "1234", "attest": True})
    assert ok.status_code == 200, ok.text


def test_unknown_future_year_uses_latest_table_and_says_so():
    from cpa_ai.rules import data
    ty = data.tax_year(2031)
    assert ty.brackets == data.TAX_YEARS[2026].brackets and ty.source.verified is False
    assert "אין טבלה לשנת 2031" in ty.source.note
    assert estimate_burden(150_000, 2031, 2.25)["tax_table_verified"] is False
