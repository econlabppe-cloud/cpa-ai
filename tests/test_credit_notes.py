"""חשבונית זיכוי מספק: מקטינה הוצאה ומע"מ תשומות — לא הוצאה נוספת."""
from datetime import date

from fastapi.testclient import TestClient

from cpa_ai.api import create_app
from cpa_ai.rules.engine import evaluate_expense

from .test_api import APPROVER, H
from .test_rules import HEB_INVOICE, codes, doc

CREDIT = HEB_INVOICE.replace("חשבונית מס מס' 10234", "חשבונית זיכוי מס' 777")


def test_engine_expense_credit_note_is_negative():
    d = evaluate_expense(doc(doc_type="credit_invoice"))
    assert d.deductible_vat == -180.0
    assert d.recognized_expense == -1000.0
    assert d.pre_vat_amount == 1000.0  # הסכומים המודפסים נשמרים כפי שהם
    assert "CREDIT_NOTE" in codes(d)
    assert "NO_VAT_DEDUCTION_DOC" not in codes(d)
    assert d.needs_human_review  # זיכוי חייב התאמה לחשבונית המקורית


def test_credit_note_with_invalid_vendor_still_reduces_expense_but_blocks():
    d = evaluate_expense(doc(doc_type="credit_invoice", vendor_id="513456789"))
    assert "VENDOR_ID_INVALID" in codes(d)
    assert d.recognized_expense < 0


def test_credit_note_reduces_vat_report_and_annual():
    c = TestClient(create_app("sqlite://", api_key="k"))
    bid = c.post("/api/v1/businesses", headers=H, json={
        "name": "ע", "vat_id": "037544129", "owner_name": "x"}).json()["id"]
    assert c.post(f"/api/v1/businesses/{bid}/documents/text", headers=H,
                  json={"text": HEB_INVOICE}).json()["status"] == "confirmed"
    cr = c.post(f"/api/v1/businesses/{bid}/documents/text", headers=H, json={"text": CREDIT})
    assert cr.status_code == 201, cr.text
    cr = cr.json()
    assert cr["extracted"]["doc_type"] == "credit_invoice"
    assert cr["status"] == "pending_review"
    r = c.post(f"/api/v1/documents/{cr['id']}/review", headers=H, json={"action": "confirm", "reviewer": "x"})
    assert r.status_code == 200, r.text

    p = c.post(f"/api/v1/businesses/{bid}/reports", headers=H,
               params={"kind": "vat_period", "year": 2026, "month": 7}).json()["payload"]
    assert p["inputs"]["other_vat"] == 0.0  # 180 - 180
    a = c.post(f"/api/v1/businesses/{bid}/reports", headers=H,
               params={"kind": "annual_estimate", "year": 2026}).json()["payload"]
    assert a["recognized_expenses_total"] == 0.0
    assert APPROVER  # ייבוא משותף


def test_credit_note_on_fixed_asset_reduces_depreciation():
    from cpa_ai.reports.periodic import _asset_cost_sign
    d = {"doc_type": "credit_invoice"}
    assert _asset_cost_sign(d) == -1
    assert _asset_cost_sign({"doc_type": "tax_invoice"}) == 1
    assert date(2026, 1, 1)


def test_bank_direction_for_credit_notes():
    from types import SimpleNamespace as NS

    from cpa_ai.banking import money_flow
    assert money_flow(NS(direction="expense", extracted={"doc_type": "tax_invoice"})) == -1
    assert money_flow(NS(direction="expense", extracted={"doc_type": "credit_invoice"})) == 1
    assert money_flow(NS(direction="income", extracted={"doc_type": "credit_invoice"})) == -1
