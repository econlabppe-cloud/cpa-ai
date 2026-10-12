import os
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from cpa_ai import backup, storage
from cpa_ai.api import create_app
from .helpers import make_pdf

H = {"X-API-Key": "k", "X-Actor": "t"}
ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_AI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CPA_AI_ENCRYPTION_KEY", raising=False)


# ── הצפנה במנוחה ───────────────────────────────────────────────────────────
def test_files_are_encrypted_on_disk_and_decrypted_on_read(monkeypatch):
    monkeypatch.setenv("CPA_AI_ENCRYPTION_KEY", Fernet.generate_key().decode())
    pdf = make_pdf()
    rel, digest = storage.save(pdf, "inv.pdf")
    on_disk = (storage.data_dir() / rel).read_bytes()
    assert on_disk.startswith(storage.MAGIC) and b"%PDF" not in on_disk and pdf[:200] not in on_disk
    assert storage.read(rel) == pdf and digest == storage.sha256(pdf)


def test_plaintext_files_saved_before_encryption_was_enabled_still_read(monkeypatch):
    rel, _ = storage.save(b"%PDF-legacy", "old.pdf")
    monkeypatch.setenv("CPA_AI_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert storage.read(rel) == b"%PDF-legacy"


def test_key_rotation_new_key_first_old_key_still_decrypts(monkeypatch):
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv("CPA_AI_ENCRYPTION_KEY", old)
    rel, _ = storage.save(b"%PDF-secret-1", "a.pdf")
    monkeypatch.setenv("CPA_AI_ENCRYPTION_KEY", f"{new},{old}")
    assert storage.read(rel) == b"%PDF-secret-1"                      # נכתב במפתח הישן
    rel2, _ = storage.save(b"%PDF-secret-2", "b.pdf")
    monkeypatch.setenv("CPA_AI_ENCRYPTION_KEY", new)
    assert storage.read(rel2) == b"%PDF-secret-2"                     # נכתב במפתח החדש
    with pytest.raises(storage.StorageError):
        storage.read(rel)                                             # המפתח הישן הוסר: לא ניתן לפענח


def test_wrong_key_and_missing_key_give_clear_errors(monkeypatch):
    monkeypatch.setenv("CPA_AI_ENCRYPTION_KEY", Fernet.generate_key().decode())
    rel, _ = storage.save(b"%PDF-x", "a.pdf")
    monkeypatch.setenv("CPA_AI_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(storage.StorageError, match="פענוח"):
        storage.read(rel)
    monkeypatch.delenv("CPA_AI_ENCRYPTION_KEY")
    with pytest.raises(storage.StorageError, match="CPA_AI_ENCRYPTION_KEY"):
        storage.read(rel)


def test_malformed_key_is_rejected_clearly(monkeypatch):
    monkeypatch.setenv("CPA_AI_ENCRYPTION_KEY", "not-a-fernet-key")
    with pytest.raises(storage.StorageError, match="לא תקין"):
        storage.save(b"x", "a.pdf")


def test_api_roundtrip_with_encryption(monkeypatch):
    monkeypatch.setenv("CPA_AI_ENCRYPTION_KEY", Fernet.generate_key().decode())
    c = TestClient(create_app("sqlite://", api_key="k"))
    bid = c.post("/api/v1/businesses", headers=H, json={"name": "א", "vat_id": "037544129", "owner_name": "ב"}).json()["id"]
    pdf = make_pdf()
    did = c.post(f"/api/v1/businesses/{bid}/documents/upload", headers=H,
                 files=[("files", ("i.pdf", pdf, "application/pdf"))]).json()["documents"][0]
    assert c.get(f"/api/v1/documents/{did}/file", headers=H).content == pdf
    files = [p for p in (storage.data_dir() / "attachments").rglob("*") if p.is_file()]
    assert files and all(f.read_bytes().startswith(storage.MAGIC) for f in files)


# ── גיבוי ושחזור ───────────────────────────────────────────────────────────
@pytest.fixture
def populated(tmp_path):
    db = tmp_path / "live.db"
    c = TestClient(create_app(f"sqlite:///{db}", api_key="k"))
    bid = c.post("/api/v1/businesses", headers=H, json={"name": "עסק גיבוי", "vat_id": "037544129", "owner_name": "ב"}).json()["id"]
    pdf = make_pdf()
    did = c.post(f"/api/v1/businesses/{bid}/documents/upload", headers=H,
                 files=[("files", ("i.pdf", pdf, "application/pdf"))]).json()["documents"][0]
    return db, pdf, did


def test_backup_verify_and_restore_roundtrip(populated, tmp_path):
    db, pdf, did = populated
    out = tmp_path / "b.zip"
    m = backup.create_backup(f"sqlite:///{db}", storage.data_dir(), out)
    assert m["entries"] and backup.verify_backup(out) == []
    new_db, new_dir = tmp_path / "restored" / "r.db", tmp_path / "restored" / "data"
    backup.restore_backup(out, new_db, new_dir)
    con = sqlite3.connect(new_db)
    assert con.execute("select name from businesses").fetchone()[0] == "עסק גיבוי"
    att = con.execute("select attachment_path from documents where id=?", (did,)).fetchone()[0]
    assert (new_dir / att).read_bytes() == pdf


def test_backup_is_a_consistent_snapshot_even_while_the_db_is_open(populated, tmp_path):
    db, _, _ = populated
    con = sqlite3.connect(db)
    con.execute("begin immediate")  # כתיבה פתוחה בזמן הגיבוי
    out = tmp_path / "b2.zip"
    try:
        backup.create_backup(f"sqlite:///{db}", storage.data_dir(), out)
    finally:
        con.rollback(); con.close()
    assert backup.verify_backup(out) == []


def test_tampered_backup_is_detected(populated, tmp_path):
    db, _, _ = populated
    out = tmp_path / "b.zip"
    backup.create_backup(f"sqlite:///{db}", storage.data_dir(), out)
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(out) as zin, zipfile.ZipFile(bad, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.startswith("files/"):
                data = data + b"tampered"
            zout.writestr(item, data)
    problems = backup.verify_backup(bad)
    assert problems and "files/" in problems[0]
    with pytest.raises(backup.BackupError):
        backup.restore_backup(bad, tmp_path / "x.db", tmp_path / "xd")


def test_restore_refuses_to_overwrite_existing_data(populated, tmp_path):
    db, _, _ = populated
    out = tmp_path / "b.zip"
    backup.create_backup(f"sqlite:///{db}", storage.data_dir(), out)
    with pytest.raises(backup.BackupError, match="קיים"):
        backup.restore_backup(out, db, storage.data_dir())            # היעד קיים
    backup.restore_backup(out, db, storage.data_dir(), overwrite=True)


def test_backup_database_is_encrypted_when_a_key_is_configured(populated, tmp_path, monkeypatch):
    db, _, _ = populated
    monkeypatch.setenv("CPA_AI_ENCRYPTION_KEY", Fernet.generate_key().decode())
    out = tmp_path / "enc.zip"
    m = backup.create_backup(f"sqlite:///{db}", storage.data_dir(), out)
    assert m["db_encrypted"] is True
    with zipfile.ZipFile(out) as z:
        assert not z.read("db.sqlite").startswith(b"SQLite format 3")
    backup.restore_backup(out, tmp_path / "e" / "r.db", tmp_path / "e" / "d")
    assert sqlite3.connect(tmp_path / "e" / "r.db").execute("select count(*) from businesses").fetchone()[0] == 1
    monkeypatch.setenv("CPA_AI_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(backup.BackupError, match="מפתח"):
        backup.restore_backup(out, tmp_path / "f" / "r.db", tmp_path / "f" / "d")


def test_unsupported_databases_are_reported_clearly(tmp_path):
    with pytest.raises(backup.BackupError, match="pg_dump"):
        backup.create_backup("postgresql://u:p@h/db", tmp_path, tmp_path / "x.zip")
    with pytest.raises(backup.BackupError, match="זיכרון"):
        backup.create_backup("sqlite://", tmp_path, tmp_path / "x.zip")


# ── CLI ────────────────────────────────────────────────────────────────────
def run(*args, env=None):
    e = {**os.environ, **(env or {}), "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, "-m", "cpa_ai", *args], capture_output=True, text=True, env=e, cwd=ROOT)


def test_cli_gen_key_prints_a_valid_fernet_key():
    r = run("gen-key")
    assert r.returncode == 0
    Fernet(r.stdout.strip().encode())


def test_cli_backup_and_restore(populated, tmp_path):
    db, pdf, did = populated
    env = {"CPA_AI_DATABASE_URL": f"sqlite:///{db}", "CPA_AI_DATA_DIR": str(storage.data_dir())}
    out = tmp_path / "cli.zip"
    assert run("backup", str(out), env=env).returncode == 0 and out.exists()
    assert run("verify-backup", str(out)).returncode == 0
    r = run("restore", str(out), "--db-path", str(tmp_path / "rr" / "r.db"), "--data-dir", str(tmp_path / "rr" / "d"))
    assert r.returncode == 0, r.stderr
    assert sqlite3.connect(tmp_path / "rr" / "r.db").execute("select count(*) from documents").fetchone()[0] == 1


def test_wrong_key_gives_503_on_download_and_does_not_break_the_accountant_package(monkeypatch):
    monkeypatch.setenv("CPA_AI_ENCRYPTION_KEY", Fernet.generate_key().decode())
    c = TestClient(create_app("sqlite://", api_key="k"))
    bid = c.post("/api/v1/businesses", headers=H, json={"name": "א", "vat_id": "037544129", "owner_name": "ב"}).json()["id"]
    did = c.post(f"/api/v1/businesses/{bid}/documents/upload", headers=H,
                 files=[("files", ("i.pdf", make_pdf(), "application/pdf"))]).json()["documents"][0]
    monkeypatch.setenv("CPA_AI_ENCRYPTION_KEY", Fernet.generate_key().decode())   # מפתח שגוי
    r = c.get(f"/api/v1/documents/{did}/file", headers=H)
    assert r.status_code == 503 and "מפתח" in r.json()["detail"]
    pkg = c.get(f"/api/v1/businesses/{bid}/accountant-package", headers=H, params={"year": 2026, "month": 7})
    assert pkg.status_code == 200
    with zipfile.ZipFile(__import__("io").BytesIO(pkg.content)) as z:
        assert f"קבצי מקור חסרים עבור מסמכים: [{did}]" in z.read("README.txt").decode("utf-8")
