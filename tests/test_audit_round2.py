"""בדיקות ביקורת סבב 2."""

from sqlalchemy import update

from cpa_ai.advisor.planner import build_plan, marginal_rate
from cpa_ai.db import AuditLog
from cpa_ai.rules.engine import classify
from cpa_ai.schemas import ExtractedDocument


def _doc(**kw):
    base = dict(doc_type="receipt", vendor_name="x", line_items=[], extraction_confidence=0.9)
    base.update(kw)
    return ExtractedDocument(**base)


def test_gym_membership_is_not_software():
    cat, _ = classify(_doc(vendor_name="פיטנס", line_items=["מנוי חודשי לחדר כושר"]))
    assert cat.key != "software"


def test_real_software_subscription_still_classified():
    cat, _ = classify(_doc(vendor_name="Microsoft", line_items=["מנוי Microsoft 365"]))
    assert cat.key == "software"


def test_audit_chain_detects_tampering(tmp_path):
    from cpa_ai.api import create_app
    from fastapi.testclient import TestClient
    url = f"sqlite:///{tmp_path}/a.db"
    c = TestClient(create_app(url, api_key="k"))
    H = {"X-API-Key": "k", "X-Actor": "t"}
    c.post("/api/v1/businesses", headers=H, json={"name": "א", "vat_id": "037544129", "owner_name": "ב"})
    c.post("/api/v1/businesses", headers=H, json={"name": "ג", "vat_id": "513456780", "owner_name": "ד"})
    assert c.get("/api/v1/audit/verify", headers=H).json()["ok"] is True

    # שינוי ישיר במסד (מחוץ ל-API) חייב להתגלות
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=create_engine(url))
    with Session() as s:
        s.execute(update(AuditLog).where(AuditLog.id == 1).values(actor="מישהו אחר"))
        s.commit()
    res = c.get("/api/v1/audit/verify", headers=H).json()
    assert res["ok"] is False and res["broken_at"] == 1


def test_marginal_rate_uses_taxable_income_after_deduction():
    # 200,000 ₪ לפני פנסיה: שיעור 31%. אחרי ניכוי הפנסיה (~22,000) ההכנסה נופלת לתחום 20%
    plan = build_plan(
        year=2026, profit=200_000, turnover=200_000, advance_rate_pct=0, estimated_tax=0,
        credit_points=2.25, existing_pension=0, existing_keren=0, material_holder=False,
        vat_blocked={"items": [], "total_vat_blocked": 0, "action": "", "basis": [], "status": "ready"},
        vehicle={"documents": 0, "vat_deducted": 0, "note": "", "status": "ready"},
    )
    pen = next(x for x in plan["strategies"] if x["id"] == "pension_self_employed")["details"]
    ker = next(x for x in plan["strategies"] if x["id"] == "keren_hishtalmut")["details"]
    taxable_after = 200_000 - pen["room_deduction"] - ker["room"]
    assert plan["marginal_rate"] == marginal_rate(taxable_after, 2026)
    assert plan["marginal_rate"] != marginal_rate(200_000, 2026)
