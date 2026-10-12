from __future__ import annotations

import json
import os
import secrets
from urllib.parse import unquote
from pathlib import Path
from typing import Literal, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from sqlalchemy import select

import threading
import uuid

from . import ingest as ingest_mod
from . import services
from .db import AuditLog, Business, Document, Report, make_sessionmaker
from .integrations.addresses import inbound_address
from .rules import data
from .schemas import (ApprovalIn, BusinessIn, BusinessOut, DocumentOut, IncomeIn, ProcessTextIn, ReportOut,
                      ReviewIn)

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 10 * 1024 * 1024


def create_app(database_url: str | None = None, api_key: str | None = None,
               users: dict[str, str] | None = None) -> FastAPI:
    """users: {מפתח: שם} — כשמוגדר, שם המשתמש ביומן נגזר מהמפתח ולא מכותרת שהלקוח שולח.
    api_key: מפתח משותף יחיד (השם ביומן הוא מה שנשלח בכותרת X-Actor)."""
    app = FastAPI(title="CPA-AI — הכנת דוחות לעוסק מורשה", version="1.0.0")
    SessionLocal = make_sessionmaker(database_url)
    app.state.SessionLocal = SessionLocal  # לשימוש עובדי רקע ובדיקות
    from .integrations import commands as wa_commands
    app.state.wa_commands = wa_commands.handle
    from .integrations.email_imap import ImapConfig, run_forever
    imap_cfg = ImapConfig.from_env()
    if imap_cfg:
        threading.Thread(target=run_forever, args=(SessionLocal, imap_cfg, int(os.getenv("CPA_AI_IMAP_INTERVAL", "120"))),
                         daemon=True, name="imap-poller").start()
    if users is None:
        users = json.loads(os.getenv("CPA_AI_USERS", "") or "{}")
    if not users:
        shared = api_key if api_key is not None else os.getenv("CPA_AI_API_KEY", "")
        users = {shared: None} if shared else {}
    if not users and os.getenv("CPA_AI_ALLOW_INSECURE") != "1":
        raise RuntimeError("לא הוגדר CPA_AI_API_KEY (או CPA_AI_USERS). לפיתוח מקומי בלבד: CPA_AI_ALLOW_INSECURE=1")

    def session():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    def actor(x_api_key: Optional[str] = Header(None), x_actor: Optional[str] = Header(None)) -> str:
        # שמות בעברית מגיעים מקודדי URL (כותרות HTTP מוגבלות ל-ISO-8859-1)
        claimed = unquote(x_actor or "anonymous")[:200]
        if not users:
            return claimed  # מצב פיתוח מפורש בלבד (CPA_AI_ALLOW_INSECURE=1)
        for k, name in users.items():
            if x_api_key and secrets.compare_digest(x_api_key, k):
                return name or claimed  # מפתח אישי — השם נגזר מהמפתח; מפתח משותף — הכותרת
        raise HTTPException(401, "מפתח API חסר או שגוי")

    @app.exception_handler(services.NotFound)
    async def _nf(_: Request, e: Exception):
        return JSONResponse({"detail": str(e)}, status_code=404)

    @app.exception_handler(services.Conflict)
    async def _cf(_: Request, e: Exception):
        return JSONResponse({"detail": str(e)}, status_code=409)

    @app.exception_handler(PermissionError)
    async def _pe(_: Request, e: Exception):
        return JSONResponse({"detail": str(e)}, status_code=403)

    @app.exception_handler(ValueError)
    async def _ve(_: Request, e: Exception):
        return JSONResponse({"detail": str(e)}, status_code=422)

    @app.exception_handler(NotImplementedError)
    async def _ni(_: Request, e: Exception):
        return JSONResponse({"detail": str(e)}, status_code=501)

    def biz_out(b: Business) -> BusinessOut:
        return BusinessOut.model_validate(b, from_attributes=True)

    def doc_out(d: Document) -> DocumentOut:
        return DocumentOut.model_validate(d, from_attributes=True)

    def rep_out(r: Report) -> ReportOut:
        return ReportOut.model_validate(r, from_attributes=True)

    # --- UI ------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "auth_required": bool(users)}

    @app.get("/api/v1/rules")
    def rules():
        return {
            "vat_rates": [{"from": d.isoformat(), "rate": r} for d, r in data.VAT_RATES],
            "vat_source": vars(data.VAT_RATE_SOURCE),
            "allocation_thresholds": [{"from": d.isoformat(), "threshold": t} for d, t in data.ALLOCATION_THRESHOLDS],
            "allocation_source": vars(data.ALLOCATION_SOURCE),
            "categories": [
                {"key": c.key, "label": c.label_he, "income_tax_recognition": c.income_tax_recognition,
                 "vat_deduction": round(c.vat_deduction, 4), "fixed_asset": c.is_fixed_asset,
                 "source": vars(c.source)}
                for c in data.CATEGORIES.values()
            ],
            "tax_years": {y: {"verified": t.source.verified, "note": t.source.note} for y, t in data.TAX_YEARS.items()},
        }

    # --- עסקים -----------------------------------------------------------------
    @app.post("/api/v1/businesses", response_model=BusinessOut, status_code=201)
    def create_business(body: BusinessIn, s=Depends(session), who=Depends(actor)):
        if s.scalar(select(Business).where(Business.vat_id == body.vat_id.zfill(9))):
            raise services.Conflict("עסק עם מספר עוסק זה כבר קיים")
        return biz_out(services.create_business(s, body, who))

    @app.get("/api/v1/businesses", response_model=list[BusinessOut])
    def list_businesses(s=Depends(session), who=Depends(actor)):
        return [biz_out(b) for b in s.scalars(select(Business).order_by(Business.id))]

    @app.get("/api/v1/businesses/{bid}", response_model=BusinessOut)
    def get_business(bid: int, s=Depends(session), who=Depends(actor)):
        return biz_out(services.get(s, Business, bid))

    @app.put("/api/v1/businesses/{bid}/consent", response_model=BusinessOut)
    def set_consent(bid: int, llm_consent: bool, s=Depends(session), who=Depends(actor)):
        return biz_out(services.update_consent(s, bid, llm_consent, who))

    # --- מסמכים ----------------------------------------------------------------
    @app.post("/api/v1/businesses/{bid}/documents/text", response_model=DocumentOut, status_code=201)
    def process_text(bid: int, body: ProcessTextIn, s=Depends(session), who=Depends(actor)):
        biz = services.get(s, Business, bid)
        item = ingest_mod.IngestItem(source="api", source_ref=f"api:{uuid.uuid4().hex}", text=body.text)
        res = ingest_mod.ingest(s, biz, item, who, mode=body.extractor)
        if not res.documents:
            if res.duplicates:
                raise services.Conflict(f"{res.duplicates[0]['reason']} (מסמך #{res.duplicates[0]['of']})")
            raise ValueError("לא זוהתה חשבונית בטקסט")
        return doc_out(res.documents[0])

    @app.post("/api/v1/businesses/{bid}/documents/upload", status_code=202)
    async def upload(bid: int, files: list[UploadFile] = File(...), s=Depends(session), who=Depends(actor)):
        """העלאה (גרירה) של קובץ אחד או יותר: PDF, תמונה, טקסט. עוברת בדיוק את אותו צינור כמו מייל ווטסאפ."""
        biz = services.get(s, Business, bid)
        out = {"documents": [], "needs_review": [], "duplicates": [], "notes": []}
        for f in files:
            content = await f.read()
            if len(content) > MAX_UPLOAD:
                out["notes"].append(f"{f.filename}: גדול מדי (מקסימום 10MB)")
                continue
            if (f.filename or "").lower().endswith(".eml"):  # "העבר כקובץ מצורף" מהמייל
                from .integrations.email_parser import parse_email
                item, _ = parse_email(content)
                item.source_ref = f"eml:{item.source_ref}"[:200]
            else:
                item = ingest_mod.IngestItem(
                    source="upload", source_ref=f"upload:{uuid.uuid4().hex}",
                    attachments=[ingest_mod.Attachment(f.filename or "file", f.content_type or "", content)])
            res = ingest_mod.ingest(s, biz, item, who)
            out["documents"] += [d.id for d in res.documents]
            out["needs_review"] += [d.id for d in res.documents if d.status == "pending_review"]
            out["duplicates"] += res.duplicates
            out["notes"] += [f"{f.filename}: {n}" for n in res.notes]
        return out

    @app.post("/api/v1/businesses/{bid}/income", response_model=DocumentOut, status_code=201)
    def add_income(bid: int, body: IncomeIn, s=Depends(session), who=Depends(actor)):
        return doc_out(services.add_income(s, bid, body, who))

    @app.get("/api/v1/businesses/{bid}/documents", response_model=list[DocumentOut])
    def list_docs(bid: int, status: Optional[str] = None, limit: int = Query(100, ge=1, le=500),
                  offset: int = Query(0, ge=0), s=Depends(session), who=Depends(actor)):
        services.get(s, Business, bid)
        return [doc_out(d) for d in services.list_documents(s, bid, status, limit, offset)]

    @app.get("/api/v1/documents/{did}", response_model=DocumentOut)
    def get_doc(did: int, s=Depends(session), who=Depends(actor)):
        return doc_out(services.get(s, Document, did))

    @app.post("/api/v1/documents/{did}/review", response_model=DocumentOut)
    def review(did: int, body: ReviewIn, s=Depends(session), who=Depends(actor)):
        return doc_out(services.review_document(s, did, body, who))

    # --- דוחות -----------------------------------------------------------------
    @app.post("/api/v1/businesses/{bid}/reports", response_model=ReportOut, status_code=201)
    def prepare(bid: int, kind: Literal["vat_period", "income_tax_advance", "annual_estimate"],
                year: int = Query(ge=2015, le=2100), month: Optional[int] = Query(None, ge=1, le=12),
                credit_points: float = Query(2.25, ge=0, le=20), s=Depends(session), who=Depends(actor)):
        return rep_out(services.prepare_report(s, bid, kind, year, month, credit_points, who))

    @app.get("/api/v1/businesses/{bid}/reports", response_model=list[ReportOut])
    def list_reports(bid: int, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                     s=Depends(session), who=Depends(actor)):
        return [rep_out(r) for r in s.scalars(
            select(Report).where(Report.business_id == bid).order_by(Report.id.desc())
            .limit(limit).offset(offset))]

    @app.get("/api/v1/reports/{rid}", response_model=ReportOut)
    def get_report(rid: int, s=Depends(session), who=Depends(actor)):
        return rep_out(services.get(s, Report, rid))

    @app.post("/api/v1/reports/{rid}/approve", response_model=ReportOut)
    def approve(rid: int, body: ApprovalIn, s=Depends(session), who=Depends(actor)):
        return rep_out(services.approve_report(s, rid, body, who))

    @app.post("/api/v1/reports/{rid}/transmit")
    def transmit(rid: int, s=Depends(session), who=Depends(actor)):
        services.transmit_report(s, rid, who)

    @app.get("/api/v1/businesses/{bid}/unified-file")
    def unified(bid: int, year: int, s=Depends(session), who=Depends(actor)):
        blob = services.unified_file_zip(s, bid, year, who)
        return Response(blob, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="unified_{bid}_{year}.zip"'})

    # --- קליטה אוטומטית: מייל ---------------------------------------------------------
    # הקריאה מגיעה מספק דואר, לא ממשתמש: הזיהוי הוא הטוקן הסודי של העסק שבכתובת.
    def _biz_by_token(s, token: str) -> Business:
        biz = s.scalar(select(Business).where(Business.inbound_token == token))
        if biz is None:
            raise HTTPException(404, "כתובת קליטה לא מוכרת")
        return biz

    def _ingest_response(res) -> dict:
        return {"skipped": res.skipped, "documents": [d.id for d in res.documents],
                "needs_review": [d.id for d in res.documents if d.status == "pending_review"],
                "duplicates": res.duplicates, "notes": res.notes}

    @app.post("/api/v1/inbound/email/{token}", status_code=202)
    async def inbound_email_raw(token: str, request: Request, s=Depends(session)):
        from .integrations.email_parser import parse_email
        raw = await request.body()
        if not raw or len(raw) > 25 * 1024 * 1024:
            raise HTTPException(413 if raw else 400, "הודעה ריקה או גדולה מדי (מקסימום 25MB)")
        biz = _biz_by_token(s, token)
        item, _ = parse_email(raw)
        return _ingest_response(ingest_mod.ingest(s, biz, item, f"email:{(item.sender or '')[:120]}"))

    @app.post("/api/v1/inbound/email", status_code=202)
    async def inbound_email_routed(request: Request, s=Depends(session)):
        """ספק דואר שמעביר את כל ההודעות לנקודה אחת: העסק נקבע לפי הטוקן שבכתובת הנמען."""
        from .integrations.email_parser import parse_email, recipient_token
        raw = await request.body()
        if not raw or len(raw) > 25 * 1024 * 1024:
            raise HTTPException(413 if raw else 400, "הודעה ריקה או גדולה מדי (מקסימום 25MB)")
        item, msg = parse_email(raw)
        token = recipient_token(msg)
        if not token:
            raise HTTPException(404, "לא נמצא טוקן עסק בכתובת הנמען")
        biz = _biz_by_token(s, token)
        return _ingest_response(ingest_mod.ingest(s, biz, item, f"email:{(item.sender or '')[:120]}"))

    @app.post("/api/v1/inbound/email/{token}/json", status_code=202)
    def inbound_email_json(token: str, payload: dict, s=Depends(session)):
        from .integrations.email_parser import item_from_json
        biz = _biz_by_token(s, token)
        item = item_from_json(payload)
        return _ingest_response(ingest_mod.ingest(s, biz, item, f"email:{(item.sender or '')[:120]}"))

    # --- קליטה אוטומטית: WhatsApp -------------------------------------------------------
    def _wa_client():
        from .integrations.whatsapp import WhatsAppClient
        return getattr(app.state, "whatsapp_client", None) or WhatsAppClient.from_env()

    @app.get("/api/v1/inbound/whatsapp", include_in_schema=False)
    def whatsapp_verify(request: Request):
        q = request.query_params
        expected = os.getenv("CPA_AI_WHATSAPP_VERIFY_TOKEN", "")
        if q.get("hub.mode") == "subscribe" and expected and secrets.compare_digest(q.get("hub.verify_token", ""), expected):
            return PlainTextResponse(q.get("hub.challenge", ""))
        raise HTTPException(403, "אימות נכשל")

    @app.post("/api/v1/inbound/whatsapp")
    async def whatsapp_inbound(request: Request, background: BackgroundTasks):
        from .integrations import whatsapp as wa
        secret = os.getenv("CPA_AI_WHATSAPP_APP_SECRET", "")
        if not secret:
            raise HTTPException(503, "WhatsApp לא מוגדר (חסר CPA_AI_WHATSAPP_APP_SECRET)")
        raw = await request.body()
        if not wa.verify_signature(raw, request.headers.get("X-Hub-Signature-256"), secret):
            raise HTTPException(403, "חתימה לא תקינה")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise HTTPException(400, "גוף הבקשה אינו JSON")
        phone_number_id = ((payload.get("entry") or [{}])[0].get("changes") or [{}])[0].get("value", {}) \
            .get("metadata", {}).get("phone_number_id", "")
        messages = wa.parse_webhook(payload)

        def work():
            client = _wa_client()
            for msg in messages:
                with SessionLocal() as s:
                    try:
                        reply = wa.handle_message(s, msg, client, getattr(app.state, "wa_commands", None))
                    except Exception as e:  # לא מפילים את שאר ההודעות; Meta ממילא קיבלה 200
                        s.rollback()
                        reply = None
                        app.state.last_wa_error = repr(e)
                    if reply and client is not None and phone_number_id:
                        client.send_text(phone_number_id, msg.sender, reply)
                    app.state.last_wa_reply = reply

        background.add_task(work)  # Meta מצפה לתשובה מהירה; העיבוד אחרי החזרת 200
        return {"status": "ok", "messages": len(messages)}

    @app.get("/api/v1/businesses/{bid}/connections")
    def connections(bid: int, s=Depends(session), who=Depends(actor)):
        """מה הלקוח צריך כדי לחבר ערוצים: כתובת מייל להעברת חשבוניות ומספר WhatsApp."""
        biz = services.get(s, Business, bid)
        return {
            "email": {"address": inbound_address(biz), "token": biz.inbound_token,
                      "token_webhook_path": f"/api/v1/inbound/email/{biz.inbound_token}",
                      "routed_webhook_path": "/api/v1/inbound/email",
                      "imap_configured": bool(os.getenv("CPA_AI_IMAP_HOST"))},
            "whatsapp": {"phone": biz.whatsapp_phone, "webhook_path": "/api/v1/inbound/whatsapp",
                         "configured": bool(os.getenv("CPA_AI_WHATSAPP_TOKEN") and os.getenv("CPA_AI_WHATSAPP_APP_SECRET"))},
        }

    @app.put("/api/v1/businesses/{bid}/whatsapp-phone")
    def set_whatsapp_phone(bid: int, phone: str = Query(min_length=7, max_length=20), s=Depends(session),
                           who=Depends(actor)):
        from .integrations.whatsapp import normalize_phone
        biz = services.get(s, Business, bid)
        norm = normalize_phone(phone)
        other = s.scalar(select(Business).where(Business.whatsapp_phone == norm, Business.id != biz.id))
        if other is not None:
            raise services.Conflict("המספר כבר משויך לעסק אחר")
        biz.whatsapp_phone = norm
        services.audit(s, who, "set_whatsapp_phone", "business", biz.id)
        s.commit()
        return {"phone": norm}

    @app.post("/api/v1/businesses/{bid}/inbound-token/rotate")
    def rotate_token(bid: int, s=Depends(session), who=Depends(actor)):
        biz = services.get(s, Business, bid)
        biz.inbound_token = secrets.token_urlsafe(16)
        services.audit(s, who, "rotate_inbound_token", "business", biz.id)
        s.commit()
        return {"inbound_address": inbound_address(biz)}

    # --- בנק: ייבוא CSV והתאמה ------------------------------------------------------------
    @app.post("/api/v1/businesses/{bid}/bank/import")
    async def bank_import(bid: int, file: UploadFile = File(...), s=Depends(session), who=Depends(actor)):
        from . import banking
        biz = services.get(s, Business, bid)
        raw = await file.read()
        if len(raw) > 5 * 1024 * 1024:
            raise HTTPException(413, "קובץ גדול מדי (מקסימום 5MB)")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1255", errors="replace")  # קידוד נפוץ בייצוא מבנקים ישראליים
        try:
            res = banking.import_csv(s, biz.id, text)
        except banking.BankCsvError as e:
            raise HTTPException(422, str(e))
        services.audit(s, who, "bank_import", "business", biz.id, **res)
        s.commit()
        return res

    @app.get("/api/v1/businesses/{bid}/bank/unmatched")
    def bank_unmatched(bid: int, s=Depends(session), who=Depends(actor)):
        from . import banking
        return banking.unmatched(s, services.get(s, Business, bid).id)

    @app.post("/api/v1/bank/{tx_id}/ignore")
    def bank_ignore(tx_id: int, s=Depends(session), who=Depends(actor)):
        from .db import BankTransaction
        tx = services.get(s, BankTransaction, tx_id)
        tx.status = "ignored"
        services.audit(s, who, "bank_ignore", "bank_transaction", tx.id)
        s.commit()
        return {"id": tx.id, "status": tx.status}

    @app.post("/api/v1/bank/{tx_id}/match")
    def bank_match(tx_id: int, document_id: int, s=Depends(session), who=Depends(actor)):
        from .db import BankTransaction
        tx = services.get(s, BankTransaction, tx_id)
        doc = services.get(s, Document, document_id)
        if doc.business_id != tx.business_id:
            raise services.Conflict("המסמך והתנועה שייכים לעסקים שונים")
        tx.matched_document_id, tx.status = doc.id, "matched"
        services.audit(s, who, "bank_match", "bank_transaction", tx.id, document_id=doc.id)
        s.commit()
        return {"id": tx.id, "status": tx.status, "document_id": doc.id}

    # --- חוויית לקוח: בית, אישור מרוכז, קובץ מקור, חבילה לרו"ח, זיכרון ספקים ---------------
    @app.get("/api/v1/businesses/{bid}/home")
    def home(bid: int, s=Depends(session), who=Depends(actor)):
        from .home import build_home
        return build_home(s, services.get(s, Business, bid))

    @app.post("/api/v1/businesses/{bid}/documents/bulk-confirm")
    def bulk_confirm(bid: int, reviewer: str = Query(min_length=1), include_unverified: bool = False,
                     s=Depends(session), who=Depends(actor)):
        services.get(s, Business, bid)
        return services.bulk_confirm(s, bid, reviewer, include_unverified)

    @app.get("/api/v1/documents/{did}/file")
    def document_file(did: int, s=Depends(session), who=Depends(actor)):
        from urllib.parse import quote
        from . import storage
        doc = services.get(s, Document, did)
        if not doc.attachment_path:
            raise HTTPException(404, "למסמך אין קובץ מקור")
        try:
            blob = storage.read(doc.attachment_path)
        except (OSError, ValueError):
            raise HTTPException(404, "קובץ המקור לא נמצא באחסון")
        services.audit(s, who, "download_original", "document", doc.id)
        s.commit()
        name = quote(doc.attachment_name or f"document-{doc.id}")
        return Response(blob, media_type=doc.attachment_mime or "application/octet-stream",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})

    @app.get("/api/v1/businesses/{bid}/accountant-package")
    def accountant_package(bid: int, year: int = Query(ge=2015, le=2100), month: int = Query(ge=1, le=12),
                           s=Depends(session), who=Depends(actor)):
        from .package import build_package
        from .reports.periodic import vat_period_bounds
        biz = services.get(s, Business, bid)
        start, end = vat_period_bounds(year, month, biz.vat_frequency)
        blob = build_package(s, biz, start, end)
        services.audit(s, who, "export_accountant_package", "business", biz.id, start=start.isoformat(), end=end.isoformat())
        s.commit()
        return Response(blob, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="package_{bid}_{start:%Y%m}-{end:%Y%m}.zip"'})

    @app.get("/api/v1/businesses/{bid}/vendor-rules")
    def vendor_rules(bid: int, s=Depends(session), who=Depends(actor)):
        from .db import VendorRule
        services.get(s, Business, bid)
        return [{"id": r.id, "vendor_id": r.vendor_id, "vendor_name": r.vendor_name, "category": r.category}
                for r in s.scalars(select(VendorRule).where(VendorRule.business_id == bid))]

    @app.delete("/api/v1/vendor-rules/{rid}")
    def delete_vendor_rule(rid: int, s=Depends(session), who=Depends(actor)):
        from .db import VendorRule
        rule = services.get(s, VendorRule, rid)
        services.audit(s, who, "delete_vendor_rule", "vendor_rule", rule.id, vendor_id=rule.vendor_id)
        s.delete(rule)
        s.commit()
        return {"deleted": rid}

    @app.post("/api/v1/reports/{rid}/mark-submitted", response_model=ReportOut)
    def mark_submitted(rid: int, reference: str = Query(min_length=1, max_length=100), s=Depends(session),
                       who=Depends(actor)):
        return rep_out(services.mark_submitted(s, rid, reference, who))

    # --- יועץ תכנון מס (לגיטימי בלבד; הפלט להכנה לשיחה עם מייצג) -------------------
    @app.get("/api/v1/businesses/{bid}/advisor")
    def advisor(bid: int, year: int = Query(ge=2015, le=2100), credit_points: float = Query(2.25, ge=0, le=20),
                existing_pension: float = Query(0, ge=0), existing_keren: float = Query(0, ge=0),
                material_holder: bool = False, s=Depends(session), who=Depends(actor)):
        from .advisor import advise
        biz = services.get(s, Business, bid)
        services.audit(s, who, "advisor_run", "business", biz.id, year=year)
        s.commit()
        return advise(s, biz, year, credit_points, existing_pension, existing_keren, material_holder)

    # --- פרטיות וביקורת -----------------------------------------------------------
    @app.get("/api/v1/businesses/{bid}/privacy/export")
    def privacy_export(bid: int, s=Depends(session), who=Depends(actor)):
        return services.privacy_export(s, bid, who)

    @app.post("/api/v1/businesses/{bid}/privacy/purge-raw")
    def privacy_purge(bid: int, s=Depends(session), who=Depends(actor)):
        return {"purged_documents": services.privacy_purge_raw(s, bid, who)}

    @app.get("/api/v1/audit/verify")
    def audit_verify(s=Depends(session), who=Depends(actor)):
        return services.verify_audit(s)

    @app.get("/api/v1/audit")
    def audit_log(limit: int = Query(100, le=1000), s=Depends(session), who=Depends(actor)):
        rows = s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))
        return [{c.name: getattr(a, c.name) for c in AuditLog.__table__.columns} for a in rows]

    return app

