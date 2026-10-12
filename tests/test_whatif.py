"""מחשבון תרחישים. כל מספר חושב ידנית (ראו הערות) כדי לאמת את הנוסחאות ולא רק את התוכנה."""
import pytest

from cpa_ai.advisor.whatif import run_what_if

BASE = dict(profit=200_000.0, year=2026, credit_points=2.25, carried_loss=0.0, donations=0.0,
            existing_pension=0.0, existing_keren=0.0)


def one(scn, **over):
    r = run_what_if(**{**BASE, **over}, scenarios=[scn])
    return r["scenarios"][0], r


def test_baseline_matches_the_burden_model():
    _, r = one({"type": "extra_income", "amount": 0})
    assert r["baseline"]["income_tax"] == pytest.approx(20_104, abs=3)


def test_pension_deposit_in_the_deduction_channel_saves_the_marginal_rate():
    # תחילה ערוץ הזיכוי (35%) כי השיעור השולי 20% נמוך ממנו; ההפקדה מעבר לתקרת הזיכוי (11,000) נכנסת לניכוי
    s, _ = one({"type": "pension", "amount": 33_000})
    assert s["details"]["credit_deposit"] == 11_000 and s["details"]["deduction_deposit"] == 22_000
    assert s["delta_income_tax"] == pytest.approx(-(11_000 * 0.35 + 22_000 * 0.20), abs=3)  # 3,850 + 4,400


def test_small_pension_deposit_goes_to_the_better_credit_channel_first():
    s, _ = one({"type": "pension", "amount": 11_000})
    assert s["details"]["credit_deposit"] == 11_000 and s["details"]["deduction_deposit"] == 0
    assert s["delta_income_tax"] == pytest.approx(-3_850, abs=1)


def test_high_earner_prefers_the_deduction_channel_first():
    s, _ = one({"type": "pension", "amount": 10_000}, profit=400_000.0)  # שולי 47% > 35%
    assert s["details"]["deduction_deposit"] == 10_000 and s["details"]["credit_deposit"] == 0


def test_pension_deposit_above_all_limits_is_not_recognized():
    s, _ = one({"type": "pension", "amount": 100_000})
    d = s["details"]
    assert d["credit_deposit"] + d["deduction_deposit"] == pytest.approx(11_000 + 22_000)
    assert d["not_recognized"] == pytest.approx(100_000 - 33_000)


def test_existing_deposits_use_up_the_room():
    s, _ = one({"type": "pension", "amount": 11_000}, existing_pension=11_000)
    assert s["details"]["credit_deposit"] == 0 and s["details"]["deduction_deposit"] == 11_000


def test_keren_hishtalmut_deposit_is_capped_at_4_5_percent():
    s, _ = one({"type": "keren", "amount": 50_000})
    assert s["details"]["recognized"] == pytest.approx(9_000) and s["details"]["not_recognized"] == pytest.approx(41_000)
    assert s["delta_income_tax"] == pytest.approx(-9_000 * 0.20, abs=2)


def test_donation_credit_and_minimum():
    assert one({"type": "donation", "amount": 10_000})[0]["delta_income_tax"] == pytest.approx(-3_500, abs=1)
    s, _ = one({"type": "donation", "amount": 100})
    assert s["delta_income_tax"] == 0 and any("207" in n for n in s["notes"])


def test_extra_income_costs_marginal_tax_and_social_security():
    # +20,000: ביטוח לאומי 18% = 3,600; מס: 7,569×20% + 10,559×31% ≈ 4,787
    s, _ = one({"type": "extra_income", "amount": 20_000})
    assert s["delta_social_security"] == pytest.approx(3_600, abs=2)
    assert s["delta_income_tax"] == pytest.approx(4_787, abs=8)


def test_equipment_purchase_depreciates_in_first_year_prorated():
    # 12,000 ₪ מחשב ב-1.10.2026: 92 ימים → 12,000×33%×92/365 ≈ 998.1
    s, _ = one({"type": "equipment", "amount": 12_000, "purchase_date": "2026-10-01", "asset_class": "computer"})
    d = s["details"]
    assert d["first_year_depreciation"] == pytest.approx(998.1, abs=0.2)
    assert d["total_depreciation_all_years"] == pytest.approx(12_000, abs=0.05)
    assert d["vat_recovered_cash"] == pytest.approx(2_160, abs=0.01)
    assert s["delta_total"] < 0


def test_equipment_bought_in_december_helps_almost_nothing_this_year():
    s, _ = one({"type": "equipment", "amount": 12_000, "purchase_date": "2026-12-28", "asset_class": "computer"})
    assert s["details"]["first_year_depreciation"] < 50
    assert any("מע\"מ" in n for n in s["notes"])  # המע"מ כן מוחזר מיד — מצוין בהערה


def test_equipment_above_allocation_threshold_requires_allocation_number():
    s, _ = one({"type": "equipment", "amount": 12_000, "purchase_date": "2026-10-01", "asset_class": "computer"})
    assert s["details"]["allocation_number_required"] is True
    assert any("הקצאה" in n for n in s["notes"])


def test_home_office_uses_room_ratio_and_is_marked_unofficial():
    s, _ = one({"type": "home_office", "business_rooms": 1, "total_rooms": 4, "annual_costs": 54_400})
    assert s["details"]["recognized_expense"] == pytest.approx(13_600)
    assert s["delta_total"] < 0 and s["verified"] is False
    assert any("חוזה" in n for n in s["notes"])


def test_combined_effect_is_computed_on_all_scenarios_together():
    r = run_what_if(**BASE, scenarios=[{"type": "pension", "amount": 11_000}, {"type": "donation", "amount": 10_000}])
    combined = r["combined"]["delta_total"]
    assert combined == pytest.approx(r["scenarios"][0]["delta_total"] + r["scenarios"][1]["delta_total"], abs=5)


def test_unknown_scenario_type_is_a_clear_error():
    with pytest.raises(ValueError):
        run_what_if(**BASE, scenarios=[{"type": "magic", "amount": 1}])


def test_negative_amounts_are_rejected():
    with pytest.raises(ValueError):
        run_what_if(**BASE, scenarios=[{"type": "donation", "amount": -5}])


def test_every_result_carries_verification_status_and_basis():
    s, r = one({"type": "pension", "amount": 5_000})
    assert s["verified"] is False and s["basis"] and r["disclaimer"]
