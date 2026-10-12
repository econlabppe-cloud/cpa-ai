import sqlite3

import pytest
from sqlalchemy import select

from cpa_ai import services, storage
from cpa_ai.db import Business, Document, make_sessionmaker
from cpa_ai.ingest import Attachment, IngestItem, ingest
from cpa_ai.schemas import BusinessIn, ReviewIn

from .helpers import INVOICE_LINES, blank_pdf, make_pdf

INVOICE_TEXT = "\n".join(INVOICE_LINES)


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_AI_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def env():
    Session = make_sessionmaker("sqlite://")
    s = Session()
    biz = services.create_business(s, BusinessIn(name="סטודיו", vat_id="037544129", owner_name="דנה"), "t")
    return s, biz


def item(ref, text=None, atts=(), source="email"):
    return IngestItem(source=source, source_ref=ref, text=text, attachments=list(atts))


def test_migration_adds_columns_to_old_database_without_losing_data(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE businesses (id INTEGER PRIMARY KEY, name VARCHAR(200), vat_id VARCHAR(9) UNIQUE,
            owner_name VARCHAR(200), vat_frequency VARCHAR(16), advance_rate_percent FLOAT,
            llm_consent BOOLEAN, created_at DATETIME);
        INSERT INTO businesses VALUES (1,'ישן','037544129','בעלים','bimonthly',0,0,'2026-01-01 00:00:00');
        CREATE TABLE documents (id INTEGER PRIMARY KEY, business_id INTEGER, direction VARCHAR(16),
            status VARCHAR(32), extractor VARCHAR(32), doc_date DATE, extracted JSON, decision JSON,
            raw_text TEXT, reviewed_by VARCHAR(200), created_at DATETIME);
        INSERT INTO documents VALUES (1,1,'expense','confirmed','heuristic','2026-03-01','{}','{}',NULL,NULL,'2026-03-01 00:00:00');
    """)
    con.commit(); con.close()

    Session = make_sessionmaker(f"sqlite:///{path}")
    with Session() as s:
        biz = s.get(Business, 1)
        assert biz.name == "ישן" and biz.inbound_token  # נתונים נשמרו, טוקן נוצר בדיעבד
        doc = s.get(Document, 1)
        assert doc.status == "confirmed" and doc.source == "manual"


def test_same_message_twice_is_processed_once(env):
    s, biz = env
    r1 = ingest(s, biz, item("m1", INVOICE_TEXT), "t")
    r2 = ingest(s, biz, item("m1", INVOICE_TEXT), "t")
    assert len(r1.documents) == 1 and r2.skipped
    assert len(list(s.scalars(select(Document)))) == 1


def test_same_invoice_from_two_channels_is_stored_once(env):
    s, biz = env
    ingest(s, biz, item("mail-1", INVOICE_TEXT, source="email"), "t")
    r = ingest(s, biz, item("wamid-9", INVOICE_TEXT, source="whatsapp"), "t")
    assert r.documents == [] and r.duplicates and "זהה" in r.duplicates[0]["reason"]
    assert len(list(s.scalars(select(Document)))) == 1


def test_same_invoice_number_with_different_file_is_duplicate(env):
    s, biz = env
    ingest(s, biz, item("a", INVOICE_TEXT), "t")
    altered = INVOICE_TEXT + "\nהודפס מחדש בתאריך 16/07/2026"
    r = ingest(s, biz, item("b", altered), "t")
    assert r.duplicates and "מספר מסמך" in r.duplicates[0]["reason"]


def test_same_vendor_date_total_without_number_is_flagged_not_dropped(env):
    s, biz = env
    base = 'קפה ארומה\nע.מ 513456780\nקבלה\n12/03/2026\nסה"כ לתשלום: 14.00\n'
    ingest(s, biz, item("r1", base), "t")
    r = ingest(s, biz, item("r2", base + "הערה\n"), "t")
    assert len(r.documents) == 1
    assert r.documents[0].status == "pending_review"
    assert any("כפילות" in f["message"] for f in r.documents[0].decision["findings"])


def test_pdf_visual_hebrew_is_read_correctly_and_original_is_stored(env):
    s, biz = env
    r = ingest(s, biz, item("p1", atts=[Attachment("inv.pdf", "application/pdf", make_pdf(style="visual"))]), "t")
    d = r.documents[0]
    assert d.extractor.startswith("pdf+")
    assert (d.decision["pre_vat_amount"], d.decision["vat_amount"], d.decision["total_amount"]) == (1000.0, 180.0, 1180.0)
    assert d.status == "confirmed"
    assert storage.read(d.attachment_path).startswith(b"%PDF")


def test_pdf_logical_hebrew_is_also_read(env):
    s, biz = env
    r = ingest(s, biz, item("p2", atts=[Attachment("inv.pdf", "application/pdf", make_pdf(style="logical"))]), "t")
    assert r.documents[0].decision["total_amount"] == 1180.0


def test_unreadable_pdf_goes_to_review_and_keeps_the_file(env):
    s, biz = env
    r = ingest(s, biz, item("p3", atts=[Attachment("scan.pdf", "application/pdf", blank_pdf())]), "t")
    d = r.documents[0]
    assert d.status == "pending_review" and d.attachment_path
    assert any("סרוק" in f["message"] for f in d.decision["findings"])


def test_image_without_extraction_is_kept_for_manual_entry(env):
    s, biz = env
    # תמונת קבלה שהמשתמש שלח בוואטסאפ: נשמרת להזנה ידנית, גם אם היא קטנה
    r = ingest(s, biz, item("i1", atts=[Attachment("receipt.jpg", "image/jpeg", b"\xff\xd8\xff fake")], source="whatsapp"), "t")
    d = r.documents[0]
    assert d.status == "pending_review" and d.extractor == "image-manual"
    assert storage.read(d.attachment_path).startswith(b"\xff\xd8")


def test_large_receipt_photo_forwarded_by_email_is_kept(env):
    s, biz = env
    photo = b"\xff\xd8\xff" + b"\x00" * 50_000  # צילום אמיתי גדול בהרבה מלוגו
    r = ingest(s, biz, item("i2", atts=[Attachment("IMG_1.jpg", "image/jpeg", photo)]), "t")
    assert len(r.documents) == 1 and r.documents[0].extractor == "image-manual"


def test_non_invoice_message_creates_nothing(env):
    s, biz = env
    r = ingest(s, biz, item("n1", "היי, מה שלומך? נתראה מחר"), "t")
    assert r.documents == [] and r.notes


def test_vendor_memory_learns_from_a_correction(env):
    s, biz = env
    first = ingest(s, biz, item("v1", INVOICE_TEXT.replace("אופיס דיפו", "חברת הדס")), "t").documents[0]
    assert first.decision["category"] == "unknown" and first.status == "pending_review"
    services.review_document(s, first.id, ReviewIn(action="confirm", reviewer="x", category_override="software"), "x")
    nxt = ingest(s, biz, item("v2", INVOICE_TEXT.replace("אופיס דיפו", "חברת הדס").replace("10234", "10235")), "t")
    d = nxt.documents[0]
    assert d.decision["category"] == "software"
    assert any(f["code"] == "VENDOR_MEMORY" for f in d.decision["findings"])


def test_storage_blocks_path_traversal():
    with pytest.raises(ValueError):
        storage.read("../../etc/passwd")
