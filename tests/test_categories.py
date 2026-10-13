"""קטגוריות הוצאה נפוצות שלא היו מכוסות: שכירות, תקשורת, חשמל, ביטוח עסק, פרסום, עמלות, הדרכה, משלוחים, אחזקה."""
import pytest

from cpa_ai.rules import data
from cpa_ai.rules.engine import evaluate_expense

from .test_rules import codes, doc


@pytest.mark.parametrize("vendor,line,expected", [
    ("בזק בינלאומי", "שירותי אינטרנט", "telecom"),
    ("סלקום", "חבילת סלולר", "telecom"),
    ("חברת החשמל", "חשבון חשמל", "utilities"),
    ("עיריית חולון", "ארנונה", "utilities"),
    ("נכסי הדר בע\"מ", "דמי שכירות למשרד", "rent"),
    ("הראל", "ביטוח אחריות מקצועית", "insurance"),
    ("Google Ireland", "Google Ads קמפיין", "advertising"),
    ("בנק הפועלים", "עמלות סליקה", "bank_fees"),
    ("אקדמיה", "קורס הכשרה מקצועי", "training"),
    ("דואר ישראל", "שירותי משלוח", "shipping"),
    ("אינסטלטור שמעון", "תיקון נזילה", "maintenance"),
])
def test_common_expense_is_classified(vendor, line, expected):
    d = evaluate_expense(doc(vendor_name=vendor, line_items=[line]))
    assert d.category == expected, (d.category, codes(d))
    assert "CATEGORY_UNKNOWN" not in codes(d)


def test_vehicle_insurance_still_goes_to_vehicle():
    d = evaluate_expense(doc(vendor_name="הפניקס", line_items=["ביטוח רכב"]))
    assert d.category == "vehicle"


def test_general_business_categories_auto_confirm_but_mixed_use_do_not():
    rent = evaluate_expense(doc(vendor_name="נכסי הדר", line_items=["דמי שכירות"]))
    assert rent.recognized_expense == 1000.0 and rent.deductible_vat == 180.0 and not rent.needs_human_review
    tel = evaluate_expense(doc(vendor_name="סלקום", line_items=["חבילה"]))
    assert tel.needs_human_review and "RULE_UNVERIFIED" in codes(tel)  # שימוש פרטי — לבדיקה


def test_gifts_never_deduct_vat_and_need_review():
    d = evaluate_expense(doc(vendor_name="פרחי הגליל", line_items=["מתנות ללקוחות שי לחג"]))
    assert d.category == "gifts" and d.deductible_vat == 0.0 and d.needs_human_review


def test_every_category_has_a_source_and_label():
    for c in data.CATEGORIES.values():
        assert c.label_he and c.source.citation
    assert {"rent", "telecom", "utilities", "insurance", "advertising", "bank_fees", "training", "shipping",
            "maintenance", "gifts", "travel"} <= set(data.CATEGORIES)
