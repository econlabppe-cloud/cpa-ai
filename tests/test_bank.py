import pytest
from fastapi.testclient import TestClient

from cpa_ai import banking
from cpa_ai.api import create_app

H = {"X-API-Key": "k", "X-Actor": "t"}

CSV = """תאריך,תיאור,חובה,זכות,יתרה
02/03/2026,סונול תחנה 12,"1,180.00",,50000
05/03/2026,העברה מלקוח בע"מ,,"11,800.00",61800
06/03/2026,WOLT TLV,230.00,,61570
06/03/2026,קפה ארומה,14.00,,61556
06/03/2026,קפה ארומה,14.00,,61542
10/03/2026,משכורת,"15,000.00",,46542
"""


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_AI_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def c():
    client = TestClient(create_app("sqlite://", api_key="k"))
    bid = client.post("/api/v1/businesses", headers=H, json={"name": "א", "vat_id": "037544129", "owner_name": "ב"}).json()["id"]
    return client, bid


def upload(client, bid, content: bytes):
    return client.post(f"/api/v1/businesses/{bid}/bank/import", headers=H, files={"file": ("bank.csv", content, "text/csv")})


def test_parse_hebrew_csv_with_debit_credit_columns():
    rows = banking.parse_csv(CSV)
    assert (rows[0][0].isoformat(), rows[0][2]) == ("2026-03-02", -1180.0)
    assert rows[1][2] == 11800.0
    assert len(rows) == 6


def test_parse_semicolon_and_single_amount_column_and_negative_formats():
    text = "date;description;amount\n2026-03-01;test;(250.50)\n2026-03-02;other;1,000.00\n2026-03-03;x;75-\n"
    amounts = [r[2] for r in banking.parse_csv(text)]
    assert amounts == [-250.5, 1000.0, -75.0]


def test_missing_header_is_a_clear_error():
    with pytest.raises(banking.BankCsvError):
        banking.parse_csv("a,b,c\n1,2,3\n")


def test_import_keeps_two_identical_same_day_purchases_and_is_idempotent(c):
    client, bid = c
    first = upload(client, bid, CSV.encode("utf-8")).json()
    assert first["imported"] == 6  # שתי קפה זהות באותו יום נשארות נפרדות
    again = upload(client, bid, CSV.encode("utf-8")).json()
    assert again["imported"] == 0 and again["skipped_duplicates"] == 6


def test_cp1255_encoded_file_is_decoded(c):
    client, bid = c
    r = upload(client, bid, CSV.encode("cp1255"))
    assert r.status_code == 200 and r.json()["imported"] == 6


def test_bad_csv_returns_422(c):
    client, bid = c
    assert upload(client, bid, b"no,header,here\n1,2,3\n").status_code == 422


def invoice(client, bid, vendor, total, date_str, number):
    pre = round(total / 1.18, 2)
    text = (f"{vendor}\nח.פ 513456780\nחשבונית מס מס' {number}\nתאריך: {date_str}\nנייר\n"
            f'סה"כ לפני מע"מ: {pre}\nמע"מ 18%: {round(total - pre, 2)}\nסה"כ לתשלום: {total}\n')
    r = client.post(f"/api/v1/businesses/{bid}/documents/text", headers=H, json={"text": text})
    assert r.status_code == 201, r.text
    return r.json()


def test_auto_match_by_amount_date_and_name(c):
    client, bid = c
    d = invoice(client, bid, "סונול", 1180.0, "01/03/2026", "7001")
    r = upload(client, bid, CSV.encode("utf-8")).json()
    assert r["auto_matched"] == 1
    un = client.get(f"/api/v1/businesses/{bid}/bank/unmatched", headers=H).json()
    ids = [t["description"] for t in un["transactions_without_document"]]
    assert "סונול תחנה 12" not in ids  # הותאם לחשבונית
    assert "WOLT TLV" in ids          # 230 ₪ בלי קבלה — מוצג ללקוח
    assert "קפה ארומה" not in ids      # 14 ₪ מתחת לסף, לא מציפים
    assert d["id"] not in [x["document_id"] for x in un["documents_without_payment"]]


def test_ambiguous_match_is_left_for_the_user(c):
    client, bid = c
    invoice(client, bid, "ספק א", 230.0, "05/03/2026", "1")
    invoice(client, bid, "ספק ב", 230.0, "05/03/2026", "2")
    r = upload(client, bid, CSV.encode("utf-8")).json()
    assert r["auto_matched"] == 0


def test_ignore_and_manual_match(c):
    client, bid = c
    upload(client, bid, CSV.encode("utf-8"))
    un = client.get(f"/api/v1/businesses/{bid}/bank/unmatched", headers=H).json()["transactions_without_document"]
    salary = next(t for t in un if t["description"] == "משכורת")
    assert client.post(f"/api/v1/bank/{salary['id']}/ignore", headers=H).json()["status"] == "ignored"
    d = invoice(client, bid, "וולט", 230.0, "20/03/2026", "9")  # תאריך רחוק: לא הותאם אוטומטית
    wolt = next(t for t in un if t["description"] == "WOLT TLV")
    assert client.post(f"/api/v1/bank/{wolt['id']}/match", headers=H, params={"document_id": d["id"]}).json()["status"] == "matched"
    left = client.get(f"/api/v1/businesses/{bid}/bank/unmatched", headers=H).json()["transactions_without_document"]
    assert not any(t["description"] in ("משכורת", "WOLT TLV") for t in left)


def test_cannot_match_across_businesses(c):
    client, bid = c
    other = client.post("/api/v1/businesses", headers=H, json={"name": "ג", "vat_id": "513456780", "owner_name": "ד"}).json()["id"]
    upload(client, bid, CSV.encode("utf-8"))
    d = invoice(client, other, "ספק", 230.0, "06/03/2026", "5")
    tx = client.get(f"/api/v1/businesses/{bid}/bank/unmatched", headers=H).json()["transactions_without_document"][0]
    assert client.post(f"/api/v1/bank/{tx['id']}/match", headers=H, params={"document_id": d["id"]}).status_code == 409
