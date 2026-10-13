"""מצב הדגמה: מערכת מלאה לצפייה, בלי מפתחות ובלי כתיבה."""
import importlib
import sys

import pytest
from fastapi.testclient import TestClient

from cpa_ai import demo
from cpa_ai.api import create_app


@pytest.fixture
def c():
    app = create_app("sqlite://", demo=True)
    demo.seed(app.state.SessionLocal)
    return TestClient(app)


def test_demo_needs_no_key_and_is_marked(c):
    assert c.get("/api/v1/health").json()["demo"] is True
    assert c.get("/api/v1/businesses").status_code == 200  # בלי X-API-Key


def test_demo_is_read_only_but_calculators_work(c):
    r = c.post("/api/v1/businesses/1/income", json={"doc_date": "2026-01-10", "pre_vat_amount": 100})
    assert r.status_code == 403 and "הדגמה" in r.json()["detail"]
    assert c.post("/api/v1/businesses/1/documents/text", json={"text": "x"}).status_code == 403
    assert c.post("/api/v1/businesses/1/threads", json={"kind": "task", "title": "t", "body": "b"}).status_code == 403
    r = c.post("/api/v1/businesses/1/advisor/optimize", json={"year": 2026, "budget": 10000, "projected_profit": 200000})
    assert r.status_code == 200 and r.json()["plan"]["total_saving"] > 0


def test_seed_shows_every_screen_with_data(c):
    h = c.get("/api/v1/businesses/1/home").json()
    assert h["counts"]["pending"] >= 4 and h["counts"]["documents"] > 15
    ids = {t["id"] for t in h["tasks"]}
    assert {"review", "bank", "threads_client", "threads_accountant", "allocation"} <= ids
    assert len(c.get("/api/v1/businesses/1/threads").json()) == 2
    reps = c.get("/api/v1/businesses/1/reports").json()
    assert any(r["status"] == "approved" and r["submission_ref"] == "DEMO-001" for r in reps)
    year = demo.date.today().year
    pnl = c.get("/api/v1/businesses/1/pnl", params={"year": year}).json()["total"]
    assert pnl["income"] > 0 or c.get("/api/v1/businesses/1/pnl", params={"year": year - 1}).json()["total"]["income"] > 0
    assert c.get("/api/v1/businesses/1/cashflow").status_code == 200
    gap = c.get("/api/v1/businesses/1/health", params={"year": year}).json()
    gap_prev = c.get("/api/v1/businesses/1/health", params={"year": year - 1}).json()
    assert any(i["code"] == "INCOME_NUMBER_GAP" for i in gap["issues"] + gap_prev["issues"])


def test_vercel_entry_runs_demo_without_keys_and_never_exposes_a_configured_db(monkeypatch):
    for k in ("CPA_AI_USERS", "CPA_AI_API_KEY", "CPA_AI_ALLOW_INSECURE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CPA_AI_DATABASE_URL", "postgresql://real:secret@db.example.com/prod")  # לא ייגע בו
    monkeypatch.setenv("CPA_AI_STORAGE", "disk")
    monkeypatch.setenv("CPA_AI_OCR", "auto")
    sys.modules.pop("cpa_ai.vercel_entry", None)
    mod = importlib.import_module("cpa_ai.vercel_entry")
    r = TestClient(mod.app).get("/api/v1/health")
    assert r.json()["demo"] is True
    sys.modules.pop("cpa_ai.vercel_entry", None)


def test_vercel_rewrite_path_is_restored(monkeypatch):
    """Vercel מנתב ל-/api/index?__p=<נתיב>; האפליקציה משחזרת את הנתיב המקורי (ושומרת פרמטרים אחרים)."""
    for k in ("CPA_AI_USERS", "CPA_AI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CPA_AI_STORAGE", "disk")
    monkeypatch.setenv("CPA_AI_OCR", "auto")
    sys.modules.pop("cpa_ai.vercel_entry", None)
    mod = importlib.import_module("cpa_ai.vercel_entry")
    c = TestClient(mod.app)
    assert c.get("/api/index?__p=").status_code == 200 and "<html" in c.get("/api/index?__p=").text.lower()
    assert c.get("/api/index?__p=api/v1/health").json()["demo"] is True
    docs = c.get("/api/index?__p=api/v1/businesses/1/documents&limit=2").json()
    assert len(docs) == 2
    assert c.get("/api/v1/health").json()["demo"] is True  # נתיב מקורי ללא שינוי
    sys.modules.pop("cpa_ai.vercel_entry", None)


def test_demo_survives_concurrent_requests():
    """מסד הדמו הוא חיבור SQLite יחיד; בקשות מקבילות היו מחזירות 500 עד שהוספנו סידור."""
    from concurrent.futures import ThreadPoolExecutor
    app = create_app("sqlite://", demo=True)
    demo.seed(app.state.SessionLocal)
    c = TestClient(app)
    paths = ["/api/v1/businesses/1/home", "/api/v1/businesses/1/documents", "/api/v1/dashboard",
             "/api/v1/businesses/1/threads", "/api/v1/businesses/1/reports"] * 6
    with ThreadPoolExecutor(10) as ex:
        codes = list(ex.map(lambda p: c.get(p).status_code, paths))
    assert set(codes) == {200}


def test_favicon_is_served():
    c = TestClient(create_app("sqlite://", demo=True))
    r = c.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg")
