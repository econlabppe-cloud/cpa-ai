"""לוח רו"ח: משימות מכל הלקוחות שמותרים לו, ממוינות לפי דחיפות. לקוח רואה רק את עצמו."""
import pytest
from fastapi.testclient import TestClient

from cpa_ai.api import create_app

USERS = {
    "adm": "מנהל",
    "acc": {"name": "רו\"ח", "role": "accountant", "businesses": [1, 2]},
    "cli": {"name": "דנה", "role": "client", "businesses": [1]},
}
TEXT = ("ספק\nח.פ 513451005\nחשבונית מס מס' {n}\nתאריך: 15/07/2026\nשירות\n"
        'סה"כ לפני מע"מ: 1000.00\nמע"מ 18%: 180.00\nסה"כ לתשלום: 1180.00\n')


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_AI_DATA_DIR", str(tmp_path / "d"))
    cl = TestClient(create_app("sqlite://", users=USERS))
    A = {"X-API-Key": "adm"}
    cl.post("/api/v1/businesses", headers=A, json={"name": "לקוחה א", "vat_id": "037544129", "owner_name": "א"})
    cl.post("/api/v1/businesses", headers=A, json={"name": "לקוח ב", "vat_id": "513451005", "owner_name": "ב"})
    cl.post("/api/v1/businesses/2/documents/text", headers=A, json={"text": TEXT.format(n=1)})
    cl.post("/api/v1/businesses/2/documents/text", headers=A, json={"text": TEXT.format(n=2)})
    return cl


def test_accountant_sees_all_assigned_clients_with_counts(c):
    d = c.get("/api/v1/dashboard", headers={"X-API-Key": "acc"}).json()
    names = {b["name"] for b in d["businesses"]}
    assert names == {"לקוחה א", "לקוח ב"}
    by = {b["name"]: b for b in d["businesses"]}
    assert by["לקוח ב"]["pending"] == 2 and by["לקוחה א"]["pending"] == 0


def test_tasks_are_sorted_by_priority_and_tagged_with_the_client(c):
    d = c.get("/api/v1/dashboard", headers={"X-API-Key": "acc"}).json()
    prios = [t["priority"] for t in d["tasks"]]
    assert prios == sorted(prios)
    assert all("business_id" in t and "business_name" in t for t in d["tasks"])
    review = [t for t in d["tasks"] if t["id"] == "review"]
    assert review and review[0]["business_name"] == "לקוח ב"


def test_client_sees_only_their_own_business(c):
    d = c.get("/api/v1/dashboard", headers={"X-API-Key": "cli"}).json()
    assert [b["name"] for b in d["businesses"]] == ["לקוחה א"]
    assert all(t["business_id"] == 1 for t in d["tasks"])


def test_admin_sees_every_business(c):
    d = c.get("/api/v1/dashboard", headers={"X-API-Key": "adm"}).json()
    assert len(d["businesses"]) == 2


def test_dashboard_requires_a_key(c):
    assert c.get("/api/v1/dashboard").status_code == 401


def test_summary_counts_urgent_items(c):
    d = c.get("/api/v1/dashboard", headers={"X-API-Key": "acc"}).json()
    assert d["summary"]["businesses"] == 2
    assert d["summary"]["pending_documents"] == 2
    assert d["summary"]["urgent_tasks"] == sum(1 for t in d["tasks"] if t["priority"] <= 2)


def test_manifest_is_public_and_installable(c):
    r = c.get("/manifest.webmanifest")  # בלי מפתח: נדרש כדי שהדפדפן יציע התקנה
    assert r.status_code == 200 and "manifest" in r.headers["content-type"]
    m = r.json()
    assert m["display"] == "standalone" and m["dir"] == "rtl" and m["lang"] == "he" and m["icons"]


def test_index_links_the_manifest(c):
    assert 'rel="manifest" href="/manifest.webmanifest"' in c.get("/").text
