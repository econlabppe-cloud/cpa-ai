"""רגרסיה: תבנית המספר חתכה סכומים ≥1000 שהודפסו בלי פסיק מפריד ('1180.00' נקרא 118)."""
import pytest

from cpa_ai.extraction import heuristic


def total_of(printed: str) -> float | None:
    return heuristic.extract(f"ספק\nח.פ 513456780\nחשבונית מס מס' 5\n10/07/2026\nסה\"כ לתשלום: {printed}\n").total_amount


@pytest.mark.parametrize("printed,expected", [
    ("1180.00", 1180.0), ("1180", 1180.0), ("1,180.00", 1180.0), ("12 500.00", 12500.0),
    ("5000.5", 5000.5), ("99.90", 99.9), ("1234567.89", 1234567.89), ("1,234,567.89", 1234567.89),
    ("118", 118.0), ("1180.00 ₪", 1180.0), ("₪ 1180.00", 1180.0),
])
def test_amount_formats(printed, expected):
    assert total_of(printed) == expected


def test_vat_and_pre_vat_amounts_without_separators():
    e = heuristic.extract('לפני מע"מ: 1000.00\nמע"מ 18%: 180.00\nסה"כ לתשלום: 1180.00\n')
    assert (e.pre_vat_amount, e.vat_amount, e.total_amount) == (1000.0, 180.0, 1180.0)
