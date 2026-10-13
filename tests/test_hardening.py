"""הגבלת קצב וכותרות אבטחה."""
import re

import pytest
from fastapi.testclient import TestClient

from cpa_ai.api import create_app
from cpa_ai.security import RateLimiter

H = {"X-API-Key": "k", "X-Actor": "t"}


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_AI_DATA_DIR", str(tmp_path / "data"))


def app_with(**limits):
    return TestClient(create_app("sqlite://", api_key="k", rate_limits=limits or None))


# ── מגביל קצב (יחידה) ──────────────────────────────────────────────────────
def test_limiter_window_slides():
    t = [0.0]
    rl = RateLimiter(clock=lambda: t[0])
    assert [rl.hit("a", 2, 10) for _ in range(2)] == [None, None]
    wait = rl.hit("a", 2, 10)
    assert wait is not None and 9 < wait <= 10
    t[0] = 10.5
    assert rl.hit("a", 2, 10) is None           # החלון זז
    assert rl.hit("b", 2, 10) is None           # מפתחות שונים בנפרד


# ── הגבלת קצב ב-API ────────────────────────────────────────────────────────
def test_api_requests_are_rate_limited_with_retry_after():
    c = app_with(api=(3, 60))
    assert [c.get("/api/v1/businesses", headers=H).status_code for _ in range(3)] == [200, 200, 200]
    r = c.get("/api/v1/businesses", headers=H)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


def test_health_check_is_not_rate_limited_so_monitors_keep_working():
    c = app_with(api=(1, 60))
    assert all(c.get("/api/v1/health").status_code == 200 for _ in range(5))


def test_inbound_endpoints_have_their_own_stricter_bucket():
    c = app_with(inbound=(2, 60))
    codes = [c.post("/api/v1/inbound/email/not-a-token", content=b"x").status_code for _ in range(4)]
    assert codes == [404, 404, 429, 429]
    assert c.get("/api/v1/businesses", headers=H).status_code == 200  # ה-API הרגיל לא נפגע


def test_repeated_wrong_keys_lock_out_guessing_even_for_a_correct_key():
    c = app_with(authfail=(3, 60))
    for _ in range(3):
        assert c.get("/api/v1/businesses", headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.get("/api/v1/businesses", headers=H).status_code == 429


def test_forwarded_for_header_cannot_be_used_to_evade_limits_by_default():
    c = app_with(api=(2, 60))
    for i in range(2):
        c.get("/api/v1/businesses", headers={**H, "X-Forwarded-For": f"10.0.0.{i}"})
    assert c.get("/api/v1/businesses", headers={**H, "X-Forwarded-For": "10.9.9.9"}).status_code == 429


def test_forwarded_for_is_honoured_only_behind_a_trusted_proxy(monkeypatch):
    monkeypatch.setenv("CPA_AI_TRUST_PROXY", "1")
    c = app_with(inbound=(1, 60))
    a = c.post("/api/v1/inbound/email/x", content=b"1", headers={"X-Forwarded-For": "1.1.1.1"}).status_code
    b = c.post("/api/v1/inbound/email/x", content=b"1", headers={"X-Forwarded-For": "2.2.2.2"}).status_code
    again = c.post("/api/v1/inbound/email/x", content=b"1", headers={"X-Forwarded-For": "1.1.1.1"}).status_code
    assert (a, b, again) == (404, 404, 429)


# ── כותרות אבטחה ───────────────────────────────────────────────────────────
def test_api_responses_carry_security_headers_and_are_not_cacheable():
    r = TestClient(create_app("sqlite://", api_key="k")).get("/api/v1/businesses", headers=H)
    h = r.headers
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer" and "no-store" in h["cache-control"]
    assert "camera=()" in h["permissions-policy"]


def test_hsts_only_over_https():
    c = TestClient(create_app("sqlite://", api_key="k"))
    assert "strict-transport-security" not in c.get("/api/v1/health").headers
    assert "max-age" in c.get("/api/v1/health", headers={"X-Forwarded-Proto": "https"}).headers["strict-transport-security"]


def test_index_csp_uses_a_fresh_nonce_and_no_unsafe_inline_scripts():
    c = TestClient(create_app("sqlite://", api_key="k"))
    a, b = c.get("/"), c.get("/")
    csp = a.headers["content-security-policy"]
    n1 = re.search(r"script-src 'nonce-([\w\-]+)'", csp).group(1)
    n2 = re.search(r"script-src 'nonce-([\w\-]+)'", b.headers["content-security-policy"]).group(1)
    assert n1 != n2
    assert "unsafe-inline" not in csp.split("style-src-attr")[0]       # רק style attr רשאי
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp and "base-uri 'none'" in csp
    scripts = re.findall(r"<script\b[^>]*>", a.text)
    assert scripts and all(f'nonce="{n1}"' in t for t in scripts)
    markup = re.sub(r"(?s)<script\b.*?</script>", "", a.text)            # קוד JS רשאי להשים onclick; מאפייני HTML לא
    assert not re.search(r"<[^>]+\son[a-z]+\s*=", markup), "נמצא event handler מוטבע ב-HTML (נחסם על ידי ה-CSP)"


def test_oversized_pasted_text_is_rejected():
    c = TestClient(create_app("sqlite://", api_key="k"))
    bid = c.post("/api/v1/businesses", headers=H, json={"name": "א", "vat_id": "037544129", "owner_name": "ב"}).json()["id"]
    r = c.post(f"/api/v1/businesses/{bid}/documents/text", headers=H, json={"text": "x" * 300_000})
    assert r.status_code == 422


def test_random_api_keys_cannot_evade_the_limit_or_grow_memory_without_bound():
    c = app_with(api=(3, 60))
    codes = [c.get("/api/v1/businesses", headers={"X-API-Key": f"random-{i}"}).status_code for i in range(6)]
    assert codes[3:] == [429, 429, 429]  # מפתח אקראי חדש בכל בקשה לא מאפס את המונה


def test_limiter_prunes_stale_buckets():
    t = [0.0]
    rl = RateLimiter(clock=lambda: t[0])
    for i in range(6000):
        rl.hit(f"k{i}", 5, 10)
    t[0] = 100.0                      # כל החלונות פגו
    rl.hit("fresh", 5, 10)
    assert len(rl._hits) < 100


def test_postgres_urls_from_providers_use_psycopg3():
    from cpa_ai.db import normalize_url
    assert normalize_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("postgresql://u:p@h/db?sslmode=require") == "postgresql+psycopg://u:p@h/db?sslmode=require"
    assert normalize_url("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("sqlite:///x.db") == "sqlite:///x.db"


def test_vercel_entrypoint_explains_broken_config(monkeypatch):
    import importlib
    import sys

    from fastapi.testclient import TestClient
    monkeypatch.setenv("CPA_AI_USERS", '{"k":"m"}')
    monkeypatch.setenv("CPA_AI_DATABASE_URL", "postgresql://u:p@127.0.0.1:1/db")  # לא זמין
    monkeypatch.setenv("CPA_AI_STORAGE", "disk")
    monkeypatch.setenv("CPA_AI_OCR", "auto")
    sys.modules.pop("cpa_ai.vercel_entry", None)
    mod = importlib.import_module("cpa_ai.vercel_entry")
    r = TestClient(mod.app).get("/")
    assert r.status_code == 503 and "לא עלתה" in r.text and "secret" not in r.text
    sys.modules.pop("cpa_ai.vercel_entry", None)
