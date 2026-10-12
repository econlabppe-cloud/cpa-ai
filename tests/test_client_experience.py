import io
import json
import zipfile
from datetime import date

import pytest
from fastapi.testclient import TestClient

from cpa_ai import home as home_mod
from cpa_ai.api import create_app
from cpa_ai.db import Business
from cpa_ai.integrations import commands

from .helpers import INVOICE_LINES, make_pdf

H = {"X-API-Key": "k", "X-Actor": "t"}
APPROVER = {"approver_name": "רו\"ח", "license_type": "cpa", "license_number": "1234", "attest": True}


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_AI_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def c():
    client = TestClient(create_app("sqlite://", api_key="k"))
    bid = client.post("/api/v1/businesses", headers=H,
                      json={"name": "סטודיו", "vat_id": "037544129", "owner_name": "דנה", "advance_rate_percent": 5}).json()["id"]
    return client, bid


def invoice_text(vendor="אופיס דיפו", vid="513456780", number="10234", date_str="15/07/2026", pre="1000.00",
                 vat="180.00", total="1,180.00", item="נייר A4"):
    return (f"{vendor}\nח.פ {vid}\nחשבונית מס מס' {number}\nתאריך: {date_str}\n{item}\n"
            f'סה"כ לפני מע"מ: {pre}\nמע"מ 18%: {vat}\nסה"כ לתשלום: {total}\n')


def add(client, bid, **kw):
    r = client.post(f"/api/v1/businesses/{bid}/documents/text", headers=H, json={"text": invoice_text(**kw)})
    assert r.status_code == 201, r.text
    return r.json()


def home(client, bid):
    return client.get(f"/api/v1/businesses/{bid}/home", headers=H).json()


def task_ids(h):
    return [t["id"] for t in h["tasks"]]


def test_new_business_home_guides_the_first_steps(c):
    client, bid = c
    h = home(client, bid)
    assert "first_doc" in task_ids(h) and "setup_whatsapp" in task_ids(h)
    assert h["all_clear"] is True  # אין משהו דחוף, רק הגדרה ראשונית


def test_pending_document_shows_up_as_a_task_with_bulk_count(c):
    client, bid = c
    # חשבונית מס בלי פירוט מע"מ: ממתינה, אבל ללא שגיאות ועם כלל מאומת → ראויה לאישור מרוכז
    client.post(f"/api/v1/businesses/{bid}/documents/text", headers=H, json={"text":
        "אופיס דיפו\nח.פ 513456780\nחשבונית מס מס' 10234\nתאריך: 15/07/2026\nנייר A4\nסה\"כ לתשלום: 1,180.00\n"})
    h = home(client, bid)
    review = next(t for t in h["tasks"] if t["id"] == "review")
    assert review["bulk_ready"] == 1 and h["counts"]["pending"] == 1
    assert h["all_clear"] is False


def test_bulk_confirm_skips_documents_that_need_a_human(c):
    client, bid = c
    client.post(f"/api/v1/businesses/{bid}/documents/text", headers=H, json={"text":
        "אופיס דיפו\nח.פ 513456780\nחשבונית מס מס' 1\nתאריך: 15/07/2026\nנייר A4\nסה\"כ לתשלום: 1,180.00\n"})
    # הוצאת דלק: כלל רכב לא מאומת → לא לאישור מרוכז
    fuel = invoice_text(vendor="סונול", vid="513451005", number="2", item="דלק 95")
    client.post(f"/api/v1/businesses/{bid}/documents/text", headers=H, json={"text": fuel})
    res = client.post(f"/api/v1/businesses/{bid}/documents/bulk-confirm", headers=H, params={"reviewer": "דנה"}).json()
    assert len(res["confirmed"]) == 1 and len(res["skipped"]) == 1
    assert "RULE_UNVERIFIED" in res["skipped"][0]["reason"]
    left = client.get(f"/api/v1/businesses/{bid}/documents", headers=H, params={"status": "pending_review"}).json()
    assert len(left) == 1
    # עם include_unverified הדלק גם מאושר — החלטה מפורשת של המשתמש
    res2 = client.post(f"/api/v1/businesses/{bid}/documents/bulk-confirm", headers=H,
                       params={"reviewer": "דנה", "include_unverified": True}).json()
    assert len(res2["confirmed"]) == 1


def test_blocked_vat_task_names_the_vendor(c):
    client, bid = c
    add(client, bid, vendor="סונול", vid="513451005", number="77", pre="6000.00", vat="1,080.00", total="7,080.00", item="דלק")
    t = next(t for t in home(client, bid)["tasks"] if t["id"] == "allocation")
    assert "1,080" in t["title"] and "סונול" in t["detail"]


def test_overdue_deadline_then_prepared_then_approved_then_submitted(c):
    client, bid = c
    today = date.today()
    # מסמך בתקופה האחרונה שהסתיימה
    s = client.app.state.SessionLocal()
    biz = s.get(Business, bid)
    p_start, p_end = home_mod.last_completed_period(biz, today)
    s.close()
    d = p_start.strftime("%d/%m/%Y")
    add(client, bid, date_str=d)
    h = home(client, bid)
    vat = next(t for t in h["tasks"] if t["id"] == "vat_deadline")
    assert "לא הוכן" in vat["detail"]

    rep = client.post(f"/api/v1/businesses/{bid}/reports", headers=H,
                      params={"kind": "vat_period", "year": p_start.year, "month": p_end.month}).json()
    assert "הדוח מוכן" in next(t for t in home(client, bid)["tasks"] if t["id"] == "vat_deadline")["detail"]

    assert client.post(f"/api/v1/reports/{rep['id']}/approve", headers=H, json=APPROVER).status_code == 200
    h = home(client, bid)
    assert "submit" in task_ids(h) and "vat_deadline" not in task_ids(h)  # מאושר ≠ הוגש: לא אומרים "הכל בסדר"

    done = client.post(f"/api/v1/reports/{rep['id']}/mark-submitted", headers=H, params={"reference": "ABC-123"})
    assert done.status_code == 200 and done.json()["submission_ref"] == "ABC-123"
    dl = home(client, bid)["deadlines"]
    assert dl[0]["status"] == "submitted" and dl[0]["submission_ref"] == "ABC-123"
    assert dl[1]["status"] == "none" and dl[1]["period_start"] > dl[0]["period_end"]  # המועד הבא, לא זה שכבר הוגש
    assert "submit" not in task_ids(home(client, bid))
    assert client.post(f"/api/v1/reports/{rep['id']}/mark-submitted", headers=H, params={"reference": "X"}).status_code == 409


def test_cannot_mark_unapproved_report_as_submitted(c):
    client, bid = c
    add(client, bid)
    rep = client.post(f"/api/v1/businesses/{bid}/reports", headers=H,
                      params={"kind": "vat_period", "year": 2026, "month": 7}).json()
    assert client.post(f"/api/v1/reports/{rep['id']}/mark-submitted", headers=H, params={"reference": "X"}).status_code == 409


def test_filing_deadline_is_the_15th_of_the_following_month():
    assert home_mod._due_date(date(2026, 8, 31)) == date(2026, 9, 15)
    assert home_mod._due_date(date(2026, 12, 31)) == date(2027, 1, 15)


def test_last_completed_period_bimonthly_and_monthly():
    class B:  # noqa
        vat_frequency = "bimonthly"
    assert home_mod.last_completed_period(B, date(2026, 10, 12)) == (date(2026, 7, 1), date(2026, 8, 31))
    B.vat_frequency = "monthly"
    assert home_mod.last_completed_period(B, date(2026, 10, 12)) == (date(2026, 9, 1), date(2026, 9, 30))
    assert home_mod.last_completed_period(B, date(2026, 1, 3)) == (date(2025, 12, 1), date(2025, 12, 31))


def test_upload_pdf_stores_original_and_it_can_be_downloaded(c):
    client, bid = c
    r = client.post(f"/api/v1/businesses/{bid}/documents/upload", headers=H,
                    files=[("files", ("inv.pdf", make_pdf(), "application/pdf"))])
    assert r.status_code == 202 and len(r.json()["documents"]) == 1
    did = r.json()["documents"][0]
    f = client.get(f"/api/v1/documents/{did}/file", headers=H)
    assert f.status_code == 200 and f.content.startswith(b"%PDF") and f.headers["content-type"] == "application/pdf"
    again = client.post(f"/api/v1/businesses/{bid}/documents/upload", headers=H,
                        files=[("files", ("inv-copy.pdf", make_pdf(), "application/pdf"))]).json()
    assert again["documents"] == [] and again["duplicates"]


def test_document_without_original_returns_404(c):
    client, bid = c
    did = add(client, bid)["id"]
    assert client.get(f"/api/v1/documents/{did}/file", headers=H).status_code == 404


def test_pasting_the_same_invoice_twice_is_rejected(c):
    client, bid = c
    add(client, bid)
    r = client.post(f"/api/v1/businesses/{bid}/documents/text", headers=H, json={"text": invoice_text()})
    assert r.status_code == 409 and "מסמך #" in r.json()["detail"]


def test_accountant_package_contents(c):
    client, bid = c
    client.post(f"/api/v1/businesses/{bid}/documents/upload", headers=H,
                files=[("files", ("inv.pdf", make_pdf(), "application/pdf"))])
    client.post(f"/api/v1/businesses/{bid}/income", headers=H, json={"doc_date": "2026-08-03", "pre_vat_amount": 4000,
                                                                      "customer_name": "לקוח"})
    r = client.get(f"/api/v1/businesses/{bid}/accountant-package", headers=H, params={"year": 2026, "month": 7})
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert {"summary.csv", "README.txt"} <= set(names) and any(n.startswith("originals/") for n in names)
    csv_text = z.read("summary.csv").decode("utf-8-sig")
    assert "אופיס דיפו" in csv_text and "הכנסה" in csv_text and "וואטסאפ" not in csv_text and "העלאה" in csv_text
    assert z.read("summary.csv").startswith(b"\xef\xbb\xbf")  # BOM: אקסל פותח עברית נכון


def test_package_includes_report_when_prepared(c):
    client, bid = c
    add(client, bid)
    client.post(f"/api/v1/businesses/{bid}/reports", headers=H, params={"kind": "vat_period", "year": 2026, "month": 7})
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/v1/businesses/{bid}/accountant-package", headers=H,
                                              params={"year": 2026, "month": 7}).content))
    rep = json.loads(z.read("report.json"))
    assert rep["kind"] == "vat_period" and rep["payload"]["sales"]["output_vat"] == 0.0


def test_vendor_memory_endpoints(c):
    client, bid = c
    d = client.post(f"/api/v1/businesses/{bid}/documents/text", headers=H,
                    json={"text": invoice_text(vendor="חברת הדס", item="שירות")}).json()
    client.post(f"/api/v1/documents/{d['id']}/review", headers=H,
                json={"action": "confirm", "reviewer": "x", "category_override": "software"})
    rules = client.get(f"/api/v1/businesses/{bid}/vendor-rules", headers=H).json()
    assert rules[0]["category"] == "software" and rules[0]["vendor_id"] == "513456780"
    assert client.delete(f"/api/v1/vendor-rules/{rules[0]['id']}", headers=H).status_code == 200
    assert client.get(f"/api/v1/businesses/{bid}/vendor-rules", headers=H).json() == []


def test_whatsapp_commands_status_and_missing(c):
    client, bid = c
    s = client.app.state.SessionLocal()
    biz = s.get(Business, bid)
    assert commands.handle(s, biz, "עזרה").startswith("אפשר לשלוח")
    status = commands.handle(s, biz, "סטטוס")
    assert "החודש" in status and "דוח מע\"מ" in status
    assert commands.handle(s, biz, "מה חסר") is not None
    assert commands.handle(s, biz, "\n".join(INVOICE_LINES)) is None  # חשבונית ולא פקודה
    s.close()


def test_eml_file_upload_is_parsed_like_an_email(c):
    from email.message import EmailMessage
    client, bid = c
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = "ספק@x.co", "me@x.co", "חשבונית", "<eml1@x>"
    m.set_content("מצורף")
    m.add_attachment(make_pdf(), maintype="application", subtype="pdf", filename="inv.pdf")
    r = client.post(f"/api/v1/businesses/{bid}/documents/upload", headers=H,
                    files=[("files", ("forwarded.eml", m.as_bytes(), "message/rfc822"))])
    assert r.status_code == 202 and len(r.json()["documents"]) == 1
    doc = client.get(f"/api/v1/documents/{r.json()['documents'][0]}", headers=H).json()
    assert doc["source"] == "email" and doc["attachment_name"] == "inv.pdf"


def test_period_labels_are_human_readable():
    assert home_mod.period_label(date(2026, 7, 1), date(2026, 8, 31)) == "יולי–אוגוסט 2026"
    assert home_mod.period_label(date(2026, 9, 1), date(2026, 9, 30)) == "ספטמבר 2026"
    assert home_mod.period_label(date(2025, 11, 1), date(2026, 12, 31)) == "נובמבר 2025–דצמבר 2026"


def test_hebrew_singular_forms_in_tasks(c):
    client, bid = c
    add(client, bid, vendor="סונול", vid="513451005", number="9", item="דלק")
    titles = [t["title"] for t in home(client, bid)["tasks"]]
    assert "מסמך אחד מחכה לבדיקה שלך" in titles


def test_future_period_is_marked_open_not_missing(c):
    client, bid = c
    dl = home(client, bid)["deadlines"]
    assert dl[0]["period_open"] is False and dl[1]["period_open"] is True


def test_year_end_task_appears_in_the_last_60_days_only_for_businesses_with_income(c):
    client, bid = c
    s = client.app.state.SessionLocal()
    try:
        biz = s.get(Business, bid)
        quiet = home_mod.build_home(s, biz, date(2026, 11, 5))
        assert "year_end" not in [t["id"] for t in quiet["tasks"]]          # אין הכנסות — אין מה לתכנן
        client.post(f"/api/v1/businesses/{bid}/income", headers=H, json={"doc_date": "2026-03-10", "pre_vat_amount": 50_000})
        early = home_mod.build_home(s, biz, date(2026, 6, 1))
        late = home_mod.build_home(s, biz, date(2026, 11, 5))
    finally:
        s.close()
    assert "year_end" not in [t["id"] for t in early["tasks"]]
    t = next(t for t in late["tasks"] if t["id"] == "year_end")
    assert "56 ימים" in t["title"] and t["cta"]["anchor"] == "m-advisor"


def test_me_endpoint_reports_role_and_signing_rights(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_AI_DATA_DIR", str(tmp_path / "d"))
    users = {"a": "מנהל", "c": {"name": "דנה", "role": "client", "businesses": [1]},
             "r": {"name": "רו\"ח", "role": "accountant", "businesses": [1]}}
    cl = TestClient(create_app("sqlite://", users=users))
    assert cl.get("/api/v1/me", headers={"X-API-Key": "a"}).json() == {
        "name": "מנהל", "role": "admin", "is_admin": True, "can_sign": True, "businesses": None}
    client_me = cl.get("/api/v1/me", headers={"X-API-Key": "c"}).json()
    assert client_me["can_sign"] is False and client_me["is_admin"] is False and client_me["businesses"] == [1]
    assert cl.get("/api/v1/me", headers={"X-API-Key": "r"}).json()["can_sign"] is True
    assert cl.get("/api/v1/me").status_code == 401
