"""אמון בערוץ המייל וזיהוי חריגות סכום."""
from sqlalchemy import select

from cpa_ai.db import Document
from cpa_ai.home import bulk_ready
from cpa_ai.ingest import ingest

from .test_ingest import INVOICE_TEXT, item



def codes(d):
    return {f["code"] for f in d.decision["findings"]}


def test_first_invoice_from_unknown_vendor_by_email_needs_review(env):
    s, biz = env
    d = ingest(s, biz, item("m1", INVOICE_TEXT, source="email"), "t").documents[0]
    assert d.status == "pending_review" and "NEW_VENDOR" in codes(d)
    assert bulk_ready(d)[0] is False
    assert d.party_id == "513456780"


def test_known_vendor_by_email_is_auto_confirmed(env):
    s, biz = env
    first = ingest(s, biz, item("m0", INVOICE_TEXT), "t").documents[0]  # הועלה ידנית (upload) ואושר
    assert first.status == "confirmed"
    nxt = INVOICE_TEXT.replace("10234", "10299").replace("15/07/2026", "15/08/2026")
    d = ingest(s, biz, item("m1", nxt, source="email"), "t").documents[0]
    assert d.status == "confirmed" and "NEW_VENDOR" not in codes(d)


def test_manual_upload_from_new_vendor_is_trusted(env):
    s, biz = env
    d = ingest(s, biz, item("u1", INVOICE_TEXT), "t").documents[0]
    assert d.status == "confirmed"


def _inv(number: str, month: int, pre: float) -> str:
    vat = round(pre * 0.18, 2)
    return (f'אופיס דיפו בע"מ\nח.פ 513456780\nחשבונית מס מס\' {number}\nתאריך: 10/{month:02d}/2026\nנייר A4\n'
            f'סה"כ לפני מע"מ: {pre:,.2f}\nמע"מ 18%: {vat:,.2f}\nסה"כ לתשלום: {pre + vat:,.2f}\n')


def test_amount_far_from_vendor_history_needs_review(env):
    s, biz = env
    for i, m in enumerate((1, 2, 3)):
        assert ingest(s, biz, item(f"h{i}", _inv(f"{i}00", m, 100 + i)), "t").documents[0].status == "confirmed"
    big = ingest(s, biz, item("big", _inv("900", 4, 1000)), "t").documents[0]
    assert big.status == "pending_review" and "AMOUNT_ANOMALY" in codes(big)
    normal = ingest(s, biz, item("ok", _inv("901", 5, 110)), "t").documents[0]
    assert normal.status == "confirmed"


def test_party_id_backfilled_by_migration(env):
    from cpa_ai.migrate import backfill_party_ids
    s, biz = env
    d = ingest(s, biz, item("m0", INVOICE_TEXT), "t").documents[0]
    d.party_id = None
    s.commit()
    assert backfill_party_ids(s.get_bind()) == 1
    s.expire_all()
    assert s.scalar(select(Document.party_id)) == "513456780"
