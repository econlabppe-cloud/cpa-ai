"""אבטחת ערוצי הקליטה: תוכן ממיילים והודעות הוא לא מהימן."""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from cpa_ai.api import create_app
from cpa_ai.integrations.whatsapp import WhatsAppClient

H = {"X-API-Key": "k", "X-Actor": "t"}


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_AI_DATA_DIR", str(tmp_path / "data"))


def test_csv_formula_injection_is_neutralized_in_accountant_package():
    c = TestClient(create_app("sqlite://", api_key="k"))
    bid = c.post("/api/v1/businesses", headers=H, json={"name": "א", "vat_id": "037544129", "owner_name": "ב"}).json()["id"]
    evil = '=HYPERLINK("http://evil.example","לחצו")'
    text = (f"{evil}\nח.פ 513456780\nחשבונית מס מס' 1\nתאריך: 15/07/2026\nנייר\n"
            'סה"כ לפני מע"מ: 1000.00\nמע"מ 18%: 180.00\nסה"כ לתשלום: 1180.00\n')
    r = c.post(f"/api/v1/businesses/{bid}/documents/text", headers=H, json={"text": text})
    assert r.status_code == 201
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/v1/businesses/{bid}/accountant-package", headers=H,
                                         params={"year": 2026, "month": 7}).content))
    rows = z.read("summary.csv").decode("utf-8-sig").splitlines()[1:]
    assert rows and all(not cell.startswith(("=", "+", "-", "@")) for row in rows for cell in row.split(","))
    assert "'=HYPERLINK" in rows[0]  # נשמר כטקסט, לא כנוסחה


@pytest.mark.parametrize("url", ["http://cdn.example/x", "ftp://cdn.example/x", "javascript:alert(1)", "//cdn.example/x"])
def test_media_download_refuses_non_https_urls_and_never_sends_the_token(url):
    calls = []

    def fetch(method, u, headers, body):
        calls.append(u)
        if u.endswith("/M1"):
            return 200, json.dumps({"url": url, "mime_type": "application/pdf"}).encode()
        return 200, b"data"

    client = WhatsAppClient("TOKEN", fetch=fetch)
    with pytest.raises(RuntimeError):
        client.download_media("M1")
    assert calls == [calls[0]] and calls[0].endswith("/M1")  # רק הבקשה ל-Graph, לא לכתובת החשודה
