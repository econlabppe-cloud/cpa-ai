from __future__ import annotations

import os
import secrets
from urllib.parse import unquote
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import select

from . import services
from .db import AuditLog, Business, Document, Report, make_sessionmaker
from .rules import data
from .schemas import (ApprovalIn, BusinessIn, BusinessOut, DocumentOut, IncomeIn, ProcessTextIn, ReportOut,
                      ReviewIn)

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 10 * 1024 * 1024


def create_app(database_url: str | None = None, api_key: str | None = None) -> FastAPI:
    app = FastAPI(title="CPA-AI — הכנת דוחות לעוסק מורשה", version="1.0.0")
    SessionLocal = make_sessionmaker(database_url)
    key = api_key if api_key is not None else os.getenv("CPA_AI_API_KEY", "")

    def session():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    def actor(x_api_key: Optional[str] = Header(None), x_actor: Optional[str] = Header(None)) -> str:
        if key and not (x_api_key and secrets.compare_digest(x_api_key, key)):
            raise HTTPException(401, "מפתח API חסר או שגוי")
        # שמות בעברית מגיעים מקודדי URL (כותרות HTTP מוגבלות ל-ISO-8859-1)
        return unquote(x_actor or "anonymous")[:200]

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
        return {"status": "ok", "auth_required": bool(key)}

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
        return doc_out(services.process_text(s, bid, body.text, body.extractor, who))

    @app.post("/api/v1/businesses/{bid}/documents/upload", response_model=DocumentOut, status_code=201)
    async def upload(bid: int, file: UploadFile = File(...), s=Depends(session), who=Depends(actor)):
        content = await file.read()
        if len(content) > MAX_UPLOAD:
            raise HTTPException(413, "קובץ גדול מדי (מקסימום 10MB)")
        mime = file.content_type or ""
        if mime.startswith("image/"):
            return doc_out(services.process_image(s, bid, content, mime, who))
        if mime.startswith("text/") or (file.filename or "").lower().endswith(".txt"):
            return doc_out(services.process_text(s, bid, content.decode("utf-8", errors="replace"), "auto", who))
        raise HTTPException(415, "נתמכים קבצי טקסט ותמונה (PDF: יש להמיר לתמונה או טקסט)")

    @app.post("/api/v1/businesses/{bid}/income", response_model=DocumentOut, status_code=201)
    def add_income(bid: int, body: IncomeIn, s=Depends(session), who=Depends(actor)):
        return doc_out(services.add_income(s, bid, body, who))

    @app.get("/api/v1/businesses/{bid}/documents", response_model=list[DocumentOut])
    def list_docs(bid: int, status: Optional[str] = None, s=Depends(session), who=Depends(actor)):
        services.get(s, Business, bid)
        return [doc_out(d) for d in services.list_documents(s, bid, status)]

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
    def list_reports(bid: int, s=Depends(session), who=Depends(actor)):
        return [rep_out(r) for r in s.scalars(
            select(Report).where(Report.business_id == bid).order_by(Report.id.desc()))]

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

    @app.get("/api/v1/audit")
    def audit_log(limit: int = Query(100, le=1000), s=Depends(session), who=Depends(actor)):
        rows = s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))
        return [{c.name: getattr(a, c.name) for c in AuditLog.__table__.columns} for a in rows]

    return app

