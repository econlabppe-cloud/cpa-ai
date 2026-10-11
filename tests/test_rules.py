from datetime import date

import pytest

from cpa_ai.extraction import heuristic
from cpa_ai.reports.periodic import income_tax, vat_period_bounds
from cpa_ai.rules import checks
from cpa_ai.rules.engine import evaluate_expense, evaluate_income
from cpa_ai.schemas import ExtractedDocument

VALID_VENDOR = "513456780"
VALID_PERSON = "037544129"


def doc(**kw) -> ExtractedDocument:
    base = dict(doc_type="tax_invoice", vendor_name="אופיס דיפו", vendor_id=VALID_VENDOR, doc_date=date(2026, 7, 15),
                pre_vat_amount=1000.0, vat_amount=180.0, total_amount=1180.0, line_items=["נייר A4"],
                extraction_confidence=0.95)
    base.update(kw)
    return ExtractedDocument(**base)


def codes(decision):
    return {f["code"] for f in decision.findings}


@pytest.mark.parametrize("raw,ok", [(VALID_VENDOR, True), (VALID_PERSON, True), ("513456789", False),
                                    ("000000000", False), ("", False), (None, False), ("12a456789", False),
                                    ("51-345678-0", True)])
def test_israeli_id(raw, ok):
    assert checks.validate_israeli_id(raw) is ok


def test_vat_rate_by_date():
    assert checks.vat_rate_for(date(2024, 12, 31)) == 0.17
    assert checks.vat_rate_for(date(2025, 1, 1)) == 0.18
    assert checks.vat_rate_for(date(2014, 1, 1)) == 0.18


def test_allocation_thresholds_by_date():
    assert checks.allocation_threshold_for(date(2024, 1, 1)) is None
    assert checks.allocation_threshold_for(date(2025, 6, 1)) == 20_000
    assert checks.allocation_threshold_for(date(2026, 5, 31)) == 10_000
    assert checks.allocation_threshold_for(date(2026, 6, 1)) == 5_000


def test_clean_office_invoice_auto_confirms():
    d = evaluate_expense(doc())
    assert d.category == "office_supplies"
    assert d.deductible_vat == 180.0
    assert d.recognized_expense == 1000.0
    assert not d.needs_human_review


def test_allocation_missing_blocks_vat_deduction():
    d = evaluate_expense(doc(pre_vat_amount=6000.0, vat_amount=1080.0, total_amount=7080.0))
    assert "ALLOCATION_MISSING" in codes(d)
    assert d.deductible_vat == 0.0
    # המע"מ שלא נוכה נכנס לעלות ההוצאה
    assert d.recognized_expense == 7080.0
    assert d.needs_human_review


def test_allocation_not_required_before_june_2026_for_6000():
    d = evaluate_expense(doc(doc_date=date(2026, 5, 20), pre_vat_amount=6000.0, vat_amount=1080.0, total_amount=7080.0))
    assert "ALLOCATION_MISSING" not in codes(d)
    assert d.deductible_vat == 1080.0


def test_allocation_present_allows_deduction():
    d = evaluate_expense(doc(pre_vat_amount=6000.0, vat_amount=1080.0, total_amount=7080.0, allocation_number="123456789"))
    assert d.deductible_vat == 1080.0
    assert "ALLOCATION_PRESENT" in codes(d)


def test_wrong_vat_rate_flagged():
    d = evaluate_expense(doc(vat_amount=170.0, total_amount=1170.0))
    assert "VAT_RATE_MISMATCH" in codes(d)
    assert d.needs_human_review


def test_2024_invoice_uses_17_percent():
    d = evaluate_expense(doc(doc_date=date(2024, 6, 1), vat_amount=170.0, total_amount=1170.0))
    assert "VAT_RATE_MISMATCH" not in codes(d)
    assert d.vat_rate == 0.17


def test_receipt_has_no_vat_deduction():
    d = evaluate_expense(doc(doc_type="receipt"))
    assert d.deductible_vat == 0.0
    assert d.recognized_expense == 1180.0


def test_invalid_vendor_id_blocks():
    d = evaluate_expense(doc(vendor_id="513456789"))
    assert "VENDOR_ID_INVALID" in codes(d)
    assert d.deductible_vat == 0.0
    assert d.needs_human_review


def test_vehicle_rule_is_unverified_and_forces_review():
    d = evaluate_expense(doc(vendor_name="סונול", line_items=["דלק 95"]))
    assert d.category == "vehicle"
    assert d.deductible_vat == 120.0
    assert d.recognized_expense == round((1000 + 60) * 0.45, 2)
    assert "RULE_UNVERIFIED" in codes(d)
    assert d.needs_human_review


def test_llm_hint_cannot_override_keyword_rule():
    d = evaluate_expense(doc(category_hint="hospitality"))
    assert d.category == "office_supplies"
    assert "CATEGORY_HINT_CONFLICT" in codes(d)


def test_manual_override():
    d = evaluate_expense(doc(), category_override="software")
    assert d.category == "software"
    with pytest.raises(ValueError):
        evaluate_expense(doc(), category_override="nope")


def test_only_total_derives_amounts():
    d = evaluate_expense(doc(pre_vat_amount=None, vat_amount=None, total_amount=1180.0))
    assert d.pre_vat_amount == 1000.0 and d.vat_amount == 180.0
    assert "AMOUNTS_DERIVED" in codes(d)


def test_income_vat_computed():
    r = evaluate_income(date(2026, 3, 1), "tax_invoice", 10_000, None, None, None)
    assert r["vat_amount"] == 1800.0
    assert r["total_amount"] == 11_800.0


def test_bimonthly_bounds():
    assert vat_period_bounds(2026, 4, "bimonthly") == (date(2026, 3, 1), date(2026, 4, 30))
    assert vat_period_bounds(2026, 2, "monthly") == (date(2026, 2, 1), date(2026, 2, 28))


def test_income_tax_brackets_2025():
    tax, _, _ = income_tax(100_000, 2025, credit_points=0)
    assert tax == pytest.approx(84_120 * 0.10 + (100_000 - 84_120) * 0.14)
    tax_cp, _, _ = income_tax(100_000, 2025, credit_points=2.25)
    assert tax_cp == pytest.approx(tax - 2.25 * 2_904)


HEB_INVOICE = """אופיס דיפו בע"מ
ח.פ 513456780
חשבונית מס מס' 10234
תאריך: 15/07/2026
נייר A4 - 10 חבילות
טונר HP
סה"כ לפני מע"מ: 1,000.00
מע"מ 18%: 180.00
סה"כ לתשלום: 1,180.00
"""


def test_heuristic_extractor_end_to_end():
    e = heuristic.extract(HEB_INVOICE)
    assert e.doc_type == "tax_invoice"
    assert e.vendor_name == 'אופיס דיפו בע"מ'
    assert e.vendor_id == "513456780"
    assert e.doc_number == "10234"
    assert e.doc_date == date(2026, 7, 15)
    assert (e.pre_vat_amount, e.vat_amount, e.total_amount) == (1000.0, 180.0, 1180.0)
    d = evaluate_expense(e)
    assert d.category == "office_supplies" and not d.needs_human_review


def test_heuristic_allocation_and_receipt_type():
    e = heuristic.extract("ספק\nחשבונית מס / קבלה\nמספר הקצאה: 987654321\nסה\"כ 500")
    assert e.doc_type == "tax_invoice_receipt"
    assert e.allocation_number == "987654321"
