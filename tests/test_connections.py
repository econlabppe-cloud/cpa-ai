"""חיבורים: דפי אשראי (CSV/Excel/HTML), API לתנועות, ויומן פקודות כפול."""
import csv
import hashlib
import hmac
import io
import json

import pytest
from fastapi.testclient import TestClient

from cpa_ai import banking
from cpa_ai.api import create_app

from .test_api import H
from .test_rules import HEB_INVOICE


@pytest.fixture
def c():
    cl = TestClient(create_app("sqlite://", api_key="k"))
    cl.post("/api/v1/businesses", headers=H, json={"name": "ע", "vat_id": "037544129", "owner_name": "x"})
    return cl


ISRACARD_CSV = ("פירוט עסקאות לכרטיס 1234\n\n"
                "תאריך רכישה,שם בית עסק,סכום עסקה,מטבע,סכום חיוב,פירוט נוסף\n"
                "16/07/2026,אופיס דיפו,1180.00,₪,1180.00,\n"
                "18/07/2026,AMAZON,30.00,$,110.50,עסקה בחו\"ל\n"
                "20/07/2026,זיכוי - אופיס דיפו,-50.00,₪,-50.00,\n")


def test_card_statement_charges_become_debits():
    rows, kind = banking.parse_statement(ISRACARD_CSV.encode())
    assert kind == "card"
    assert [(r[1], r[2]) for r in rows] == [("אופיס דיפו", -1180.0), ("AMAZON", -110.5), ("זיכוי - אופיס דיפו", 50.0)]


def test_xlsx_statement_is_read():
    from datetime import datetime

    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["דוח עסקאות"])
    ws.append(["תאריך עסקה", "שם בית העסק", "סכום חיוב"])
    ws.append([datetime(2026, 7, 16), "אופיס דיפו", 1180])
    buf = io.BytesIO()
    wb.save(buf)
    rows, kind = banking.parse_statement(buf.getvalue())
    assert kind == "card" and rows[0][2] == -1180.0 and rows[0][0].isoformat() == "2026-07-16"


def test_html_disguised_as_xls_is_read():
    html = ("<html><body><table><tr><td>תאריך עסקה</td><td>שם בית העסק</td><td>סכום חיוב</td></tr>"
            "<tr><td>16/07/2026</td><td>אופיס דיפו</td><td>1,180.00</td></tr></table></body></html>")
    rows, kind = banking.parse_statement(html.encode("utf-8"))
    assert rows[0][2] == -1180.0 and kind == "card"


def test_card_import_matches_invoice_and_tags_account(c):
    c.post("/api/v1/businesses/1/documents/text", headers=H, json={"text": HEB_INVOICE})
    r = c.post("/api/v1/businesses/1/bank/import", headers=H,
               files={"file": ("isracard.csv", ISRACARD_CSV.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json()["account"] == "card" and r.json()["auto_matched"] == 1
    un = c.get("/api/v1/businesses/1/bank/unmatched", headers=H).json()
    assert any(t["account"] == "card" and t["description"] == "AMAZON" for t in un["transactions_without_document"])


def test_old_binary_xls_gives_clear_error(c):
    r = c.post("/api/v1/businesses/1/bank/import", headers=H,
               files={"file": ("x.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100, "application/vnd.ms-excel")})
    assert r.status_code == 422 and "xlsx" in r.json()["detail"]


TXS = {"transactions": [
    {"date": "2026-07-16", "description": "אופיס דיפו", "amount": -1180, "account": "card", "external_id": "t-1"},
    {"date": "2026-07-17", "description": "העברה מלקוח", "amount": 5000, "external_id": "t-2"},
]}


def test_transactions_api_with_key_is_idempotent(c):
    r = c.post("/api/v1/businesses/1/bank/transactions", headers=H, json=TXS)
    assert r.status_code == 200 and r.json()["imported"] == 2
    assert c.post("/api/v1/businesses/1/bank/transactions", headers=H, json=TXS).json()["skipped_duplicates"] == 2


def test_transactions_webhook_requires_signature(c, monkeypatch):
    tok = c.get("/api/v1/businesses/1/connections", headers=H).json()["email"]["token"]
    body = json.dumps(TXS).encode()
    assert c.post(f"/api/v1/inbound/transactions/{tok}", content=body).status_code == 503  # לא הוגדר סוד
    monkeypatch.setenv("CPA_AI_TX_WEBHOOK_SECRET", "s3")
    assert c.post(f"/api/v1/inbound/transactions/{tok}", content=body,
                  headers={"X-CPA-Signature": "sha256=bad"}).status_code == 403
    sig = "sha256=" + hmac.new(b"s3", body, hashlib.sha256).hexdigest()
    r = c.post(f"/api/v1/inbound/transactions/{tok}", content=body, headers={"X-CPA-Signature": sig})
    assert r.status_code == 200 and r.json()["imported"] == 2
    assert c.post("/api/v1/inbound/transactions/nope", content=body, headers={"X-CPA-Signature": sig}).status_code == 404


def test_journal_is_balanced_and_maps_accounts(c):
    c.post("/api/v1/businesses/1/documents/text", headers=H, json={"text": HEB_INVOICE})
    c.post("/api/v1/businesses/1/income", headers=H, json={"doc_date": "2026-07-20", "pre_vat_amount": 4000,
                                                            "customer_name": "=HYPERLINK(\"x\")"})
    r = c.get("/api/v1/businesses/1/export/journal", headers=H, params={"year": 2026})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))
    by_doc = {}
    for row in rows:
        d = by_doc.setdefault(row["document_id"], [0.0, 0.0])
        d[0] += float(row["debit"] or 0)
        d[1] += float(row["credit"] or 0)
    assert len(by_doc) == 2 and all(abs(dr - cr) < 0.01 for dr, cr in by_doc.values())
    accounts = {row["account_name"] for row in rows}
    assert {"מע\"מ תשומות", "מע\"מ עסקאות", "הכנסות", "ספקים", "לקוחות"} <= accounts
    assert not any(row["description"].startswith("=") for row in rows)  # הגנת CSV injection
