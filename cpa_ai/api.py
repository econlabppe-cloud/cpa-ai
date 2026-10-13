from __future__ import annotations

import json
import os
import secrets
from datetime import date
from urllib.parse import unquote
from pathlib import Path
from typing import Literal, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from sqlalchemy import select

import threading
import uuid

from . import extraction
from . import ingest as ingest_mod
from . import services
from .db import AuditLog, Business, Document, Report, make_sessionmaker
from .integrations.addresses import inbound_address
from .rules import data
from .security import Principal, RateLimiter, parse_users
from .schemas import (ApprovalIn, BusinessIn, BusinessOut, DocumentOut, IncomeIn, ProcessTextIn, ReportOut,
                      MarkPaidIn, OptimizeIn, ProfileIn, ReplyIn, ReviewIn, ThreadIn, TxBatchIn, WhatIfIn)

STATIC = Path(__file__).parent / "static"
SAFE_SERVE_MIMES = {"application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp", "text/plain"}
MAX_UPLOAD = 10 * 1024 * 1024


DEFAULT_RATE_LIMITS = {"api": (600, 60), "inbound": (120, 60), "authfail": (20, 300)}


def create_app(database_url: str | None = None, api_key: str | None = None,
               users: dict | None = None, rate_limits: dict | None = None) -> FastAPI:
    """users: {מפתח: "שם"} (מנהל) או {מפתח: {"name","role","businesses"}}. השם ביומן נגזר מהמפתח ולא
    מכותרת שהלקוח שולח. api_key: מפתח משותף יחיד (מנהל; השם הוא מה שנשלח ב-X-Actor)."""
    SessionLocal = make_sessionmaker(database_url)
    limits = {**DEFAULT_RATE_LIMITS, **(rate_limits or {})}
    limiter = RateLimiter()
    trust_proxy = os.getenv("CPA_AI_TRUST_PROXY") == "1"

    def client_ip(request: Request) -> str:
        # X-Forwarded-For ניתן לזיוף; מכבדים אותו רק מאחורי פרוקסי שהוגדר כאמין
        if trust_proxy:
            fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            if fwd:
                return fwd
        return request.client.host if request.client else "unknown"

    def too_many(wait: float) -> HTTPException:
        return HTTPException(429, "יותר מדי בקשות — נסו שוב בעוד רגע", headers={"Retry-After": str(int(wait) + 1)})

    if users is None:
        users = json.loads(os.getenv("CPA_AI_USERS", "") or "{}")
    principals = parse_users(users)
    if not principals:
        shared = api_key if api_key is not None else os.getenv("CPA_AI_API_KEY", "")
        principals = {shared: Principal(None, "admin", None)} if shared else {}
    if not principals and os.getenv("CPA_AI_ALLOW_INSECURE") != "1":
        raise RuntimeError("לא הוגדר CPA_AI_API_KEY (או CPA_AI_USERS). לפיתוח מקומי בלבד: CPA_AI_ALLOW_INSECURE=1")

    def session():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    def authenticate(request: Request, x_api_key: Optional[str], x_actor: Optional[str]) -> Principal:
        cached = getattr(request.state, "principal", None)
        if cached is not None:
            return cached
        ip = client_ip(request)
        wait = limiter.peek(f"authfail:{ip}", *limits["authfail"])
        if wait is not None:
            raise too_many(wait)  # נעילה אחרי ניסיונות כושלים חוזרים — גם למפתח נכון, מאותה כתובת
        # שמות בעברית מגיעים מקודדי URL (כותרות HTTP מוגבלות ל-ISO-8859-1)
        claimed = unquote(x_actor or "anonymous")[:200]
        found: Principal | None = None
        if not principals:
            found = Principal(claimed, "admin", None)  # מצב פיתוח מפורש בלבד (CPA_AI_ALLOW_INSECURE=1)
        else:
            for k, p in principals.items():
                if x_api_key and secrets.compare_digest(x_api_key, k):
                    found = p if p.name else p.with_name(claimed)  # מפתח אישי — השם מהמפתח
                    break
        if found is None:
            limiter.hit(f"authfail:{ip}", *limits["authfail"])
            raise HTTPException(401, "מפתח API חסר או שגוי")
        request.state.principal = found
        return found

    def principal(request: Request, x_api_key: Optional[str] = Header(None),
                  x_actor: Optional[str] = Header(None)) -> Principal:
        return authenticate(request, x_api_key, x_actor)

    def actor(p: Principal = Depends(principal)) -> str:
        return p.name or "anonymous"

    def require_admin(p: Principal = Depends(principal)) -> Principal:
        if not p.is_admin:
            raise HTTPException(403, "פעולה למנהל המערכת בלבד")
        return p

    def require_signer(p: Principal = Depends(principal)) -> Principal:
        if not p.can_sign:
            raise HTTPException(403, "אישור והגשת דוח שמורים למייצג מורשה (רו\"ח, יועץ מס או עו\"ד)")
        return p

    OPEN_PREFIXES = ("/api/v1/inbound/", "/api/v1/health")

    def scope(request: Request, x_api_key: Optional[str] = Header(None), x_actor: Optional[str] = Header(None),
              s=Depends(session)) -> None:
        """שער הרשאה לכל נקודת קצה: מזהה את העסק שהבקשה נוגעת בו (מהנתיב או מהישות) ובודק גישה.
        נקודה חדשה שמקבלת bid/did/rid/tx_id מוגנת אוטומטית. 404 ולא 403: לא מגלים שעסק קיים."""
        path = request.url.path
        if not path.startswith("/api/v1/") or path.startswith(OPEN_PREFIXES):
            return
        p = authenticate(request, x_api_key, x_actor)
        pp = request.path_params

        def as_int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        bid = as_int(pp.get("bid"))
        if bid is None:
            from .db import BankTransaction, VendorRule
            from .db import Thread
            model, key = (Document, "did") if "did" in pp else (BankTransaction, "tx_id") if "tx_id" in pp else \
                (Thread, "thread_id") if "thread_id" in pp else \
                (VendorRule if "/vendor-rules/" in path else Report, "rid") if "rid" in pp else (None, None)
            if model is not None:
                ent = s.get(model, as_int(pp.get(key)) or 0)
                bid = ent.business_id if ent is not None else None
        if bid is not None and not p.can_access(bid):
            services.audit(s, p.name or "anonymous", "access_denied", "business", bid,
                           business_id=bid, method=request.method, path=path)
            s.commit()
            raise HTTPException(404, "לא נמצא")

    app = FastAPI(title="CPA-AI — הכנת דוחות לעוסק מורשה", version="1.0.0", dependencies=[Depends(scope)])
    app.state.SessionLocal = SessionLocal  # לשימוש עובדי רקע ובדיקות
    from . import storage as _storage
    _storage.configure(SessionLocal)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        ip = client_ip(request)
        if path.startswith("/api/v1/inbound/"):
            bucket, key = "inbound", f"inbound:{ip}"
        elif path.startswith("/api/") and path != "/api/v1/health":
            # לפי כתובת, לא לפי המפתח שבכותרת: מפתח אקראי חדש בכל בקשה היה מאפס את המונה ומנפח זיכרון
            bucket, key = "api", f"api:{ip}"
        else:
            bucket = key = None
        if bucket:
            wait = limiter.hit(key, *limits[bucket])
            if wait is not None:
                resp = JSONResponse({"detail": "יותר מדי בקשות — נסו שוב בעוד רגע"}, status_code=429,
                                    headers={"Retry-After": str(int(wait) + 1)})
                return _secure(resp, request, path)
        return _secure(await call_next(request), request, path)

    def _secure(resp, request: Request, path: str):
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        if path.startswith("/api/"):
            h.setdefault("Cache-Control", "no-store")
        if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return resp
    from .integrations import commands as wa_commands
    app.state.wa_commands = wa_commands.handle
    from .integrations.email_imap import ImapConfig, run_forever
    imap_cfg = ImapConfig.from_env()
    if imap_cfg:
        threading.Thread(target=run_forever, args=(SessionLocal, imap_cfg, int(os.getenv("CPA_AI_IMAP_INTERVAL", "120"))),
                         daemon=True, name="imap-poller").start()

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
        """הדף נשלח עם nonce חדש לכל בקשה: רק הסקריפטים שלנו רצים, וגם אם מוזרק HTML הוא לא יריץ קוד."""
        nonce = secrets.token_urlsafe(16)
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        html = html.replace("<script>", f'<script nonce="{nonce}">').replace("<style>", f'<style nonce="{nonce}">')
        csp = ("default-src 'none'; "
               f"script-src 'nonce-{nonce}'; style-src-elem 'nonce-{nonce}'; style-src-attr 'unsafe-inline'; "
               "connect-src 'self'; img-src 'self' data: blob:; font-src 'self'; "
               "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'")
        return HTMLResponse(html, headers={"Content-Security-Policy": csp, "Cache-Control": "no-store"})

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "auth_required": bool(principals)}

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        """התקנה על מסך הבית בטלפון. האייקון מוטמע כדי שלא תהיה בקשת רשת נוספת."""
        icon = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
                "%3Crect width='32' height='32' rx='7' fill='%232457e6'/%3E%3Cpath d='M9 17l5 5 9-11' stroke='white' "
                "stroke-width='3.5' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E")
        return JSONResponse({
            "name": "CPA-AI — הנהלת חשבון", "short_name": "CPA-AI", "lang": "he", "dir": "rtl",
            "start_url": "/", "display": "standalone", "background_color": "#f4f6fb", "theme_color": "#2457e6",
            "icons": [{"src": icon, "sizes": "any", "type": "image/svg+xml", "purpose": "any"}]},
            media_type="application/manifest+json")

    @app.get("/api/v1/me")
    def me(p: Principal = Depends(principal)):
        """מי אני ומה מותר לי — כדי שהממשק יציג רק פעולות שמותרות."""
        return {"name": p.name, "role": p.role, "is_admin": p.is_admin, "can_sign": p.can_sign,
                "businesses": sorted(p.businesses) if p.businesses is not None else None}

    @app.get("/api/v1/dashboard")
    def dashboard(s=Depends(session), p: Principal = Depends(principal)):
        """לוח משותף: משימות מכל העסקים שמותרים למשתמש, ממוינות לפי דחיפות. לא שומר מצב."""
        from .home import build_home
        rows = [b for b in s.scalars(select(Business).order_by(Business.id)) if p.can_access(b.id)]
        businesses, tasks = [], []
        for b in rows:
            h = build_home(s, b)
            pending = h["counts"]["pending"]
            businesses.append({"id": b.id, "name": b.name, "pending": pending, "bulk_ready": h["counts"]["bulk_ready"],
                               "bank_open": h["counts"]["bank_open"], "all_clear": h["all_clear"],
                               "threads": h["threads"], "tags": b.tags,
                               "deadline": h["deadlines"][0]})
            for t in h["tasks"]:
                tasks.append({**t, "business_id": b.id, "business_name": b.name})
        tasks.sort(key=lambda t: (t["priority"], t["business_name"]))
        return {
            "businesses": businesses,
            "tasks": tasks,
            "summary": {"businesses": len(rows), "pending_documents": sum(x["pending"] for x in businesses),
                        "urgent_tasks": sum(1 for t in tasks if t["priority"] <= 2),
                        "clients_all_clear": sum(1 for x in businesses if x["all_clear"])},
        }

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
    def create_business(body: BusinessIn, s=Depends(session), who=Depends(actor), _admin=Depends(require_admin)):
        if s.scalar(select(Business).where(Business.vat_id == body.vat_id.zfill(9))):
            raise services.Conflict("עסק עם מספר עוסק זה כבר קיים")
        return biz_out(services.create_business(s, body, who))

    @app.get("/api/v1/businesses", response_model=list[BusinessOut])
    def list_businesses(s=Depends(session), who=Depends(actor), p: Principal = Depends(principal)):
        return [biz_out(b) for b in s.scalars(select(Business).order_by(Business.id)) if p.can_access(b.id)]

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
    async def upload(bid: int, files: list[UploadFile] = File(...), s=Depends(session),
                     p: Principal = Depends(principal)):
        """העלאה (גרירה) של קובץ אחד או יותר: PDF, תמונה, טקסט. עוברת בדיוק את אותו צינור כמו מייל ווטסאפ."""
        from . import collab
        biz = services.get(s, Business, bid)
        out = await _ingest_files(s, biz, files, p.name or "anonymous")
        if p.role == "client" and out["documents"]:  # הלקוח העלה — עונה לבקשת מסמכים פתוחה
            collab.on_documents(s, biz, [s.get(Document, i) for i in out["documents"]], "upload", p.name or "client")
            s.commit()
        return out

    async def _ingest_files(s, biz: Business, files: list[UploadFile], who: str) -> dict:
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
                  offset: int = Query(0, ge=0), q: Optional[str] = Query(None, max_length=100),
                  direction: Optional[Literal["income", "expense"]] = None, date_from: Optional[date] = None,
                  date_to: Optional[date] = None, s=Depends(session), who=Depends(actor)):
        """q: חיפוש חופשי בשם ספק/לקוח, מספר מסמך, מספר עוסק, קטגוריה, פריטים וסכום."""
        services.get(s, Business, bid)
        return [doc_out(d) for d in services.list_documents(s, bid, status, limit, offset, q=q, direction=direction,
                                                            date_from=date_from, date_to=date_to)]

    @app.post("/api/v1/documents/{did}/reopen", response_model=DocumentOut)
    def reopen(did: int, s=Depends(session), who=Depends(actor)):
        return doc_out(services.reopen_document(s, did, who))

    @app.get("/api/v1/businesses/{bid}/parties")
    def parties(bid: int, year: int = Query(ge=2015, le=2100), s=Depends(session), who=Depends(actor)):
        from . import insights
        return insights.parties(s, services.get(s, Business, bid), year)

    @app.get("/api/v1/businesses/{bid}/cashflow")
    def cashflow(bid: int, months: int = Query(3, ge=1, le=12), today: Optional[date] = Query(None),
                 s=Depends(session), who=Depends(actor)):
        from . import insights
        return insights.cashflow(s, services.get(s, Business, bid), today, months)

    @app.get("/api/v1/businesses/{bid}/health")
    def books_health(bid: int, year: int = Query(ge=2015, le=2100), s=Depends(session), who=Depends(actor)):
        from . import health
        return health.books_health(s, services.get(s, Business, bid), year)

    @app.get("/api/v1/businesses/{bid}/year-close")
    def year_close(bid: int, year: int = Query(ge=2015, le=2100), today: Optional[date] = Query(None),
                   s=Depends(session), who=Depends(actor)):
        from . import closing
        return closing.year_close(s, services.get(s, Business, bid), year, today)

    @app.get("/api/v1/businesses/{bid}/export/annual-xlsx")
    def export_annual_xlsx(bid: int, year: int = Query(ge=2015, le=2100), s=Depends(session), who=Depends(actor)):
        from . import package
        biz = services.get(s, Business, bid)
        services.audit(s, who, "export_annual_xlsx", "business", biz.id, year=year)
        s.commit()
        return Response(package.annual_workbook(s, biz, year),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="annual_{biz.id}_{year}.xlsx"'})

    @app.get("/api/v1/documents/{did}", response_model=DocumentOut)
    def get_doc(did: int, s=Depends(session), who=Depends(actor)):
        return doc_out(services.get(s, Document, did))

    @app.post("/api/v1/documents/{did}/mark-paid", response_model=DocumentOut)
    def mark_paid(did: int, body: MarkPaidIn, s=Depends(session), who=Depends(actor)):
        return doc_out(services.mark_paid(s, did, body.paid_on, body.unpaid, who))

    @app.get("/api/v1/businesses/{bid}/receivables")
    def receivables(bid: int, today: Optional[date] = Query(None), s=Depends(session), who=Depends(actor)):
        from . import insights
        return insights.receivables(s, services.get(s, Business, bid), today)

    @app.get("/api/v1/businesses/{bid}/pnl")
    def pnl(bid: int, year: int = Query(ge=2015, le=2100), s=Depends(session), who=Depends(actor)):
        from . import insights
        return insights.monthly_pnl(s, services.get(s, Business, bid), year)

    @app.post("/api/v1/documents/{did}/review", response_model=DocumentOut)
    def review(did: int, body: ReviewIn, s=Depends(session), who=Depends(actor)):
        return doc_out(services.review_document(s, did, body, who))

    # --- דוחות -----------------------------------------------------------------
    @app.post("/api/v1/businesses/{bid}/reports", response_model=ReportOut, status_code=201)
    def prepare(bid: int, kind: Literal["vat_period", "income_tax_advance", "annual_estimate"],
                year: int = Query(ge=2015, le=2100), month: Optional[int] = Query(None, ge=1, le=12),
                credit_points: float = Query(2.25, ge=0, le=20), donations: float = Query(0, ge=0),
                carried_loss: float = Query(0, ge=0), s=Depends(session), who=Depends(actor)):
        return rep_out(services.prepare_report(s, bid, kind, year, month, credit_points, who, donations, carried_loss))

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
    def approve(rid: int, body: ApprovalIn, s=Depends(session), who=Depends(actor), _signer=Depends(require_signer)):
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
            "transactions": {"webhook_path": f"/api/v1/inbound/transactions/{biz.inbound_token}",
                             "signed": bool(os.getenv("CPA_AI_TX_WEBHOOK_SECRET"))},
            "extraction": {**extraction.capabilities(), "llm_consent": biz.llm_consent},
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
            rows, kind = banking.parse_statement(raw)
        except banking.BankCsvError as e:
            raise HTTPException(422, str(e))
        except Exception:  # noqa: BLE001 — קובץ Excel פגום
            raise HTTPException(422, "לא ניתן לקרוא את הקובץ. נסו לשמור אותו כ-CSV או xlsx")
        res = banking.import_rows(s, biz.id, rows, kind)
        services.audit(s, who, "bank_import", "business", biz.id, **res)
        s.commit()
        return res

    def _import_tx_batch(s, biz: Business, body: TxBatchIn, who: str) -> dict:
        from . import banking
        res = {"rows": 0, "imported": 0, "skipped_duplicates": 0, "auto_matched": 0}
        for account in ("bank", "card"):
            txs = [t for t in body.transactions if t.account == account]
            if txs:
                r = banking.import_rows(s, biz.id, [(t.date, t.description, round(t.amount, 2)) for t in txs],
                                        account, [t.external_id for t in txs])
                for k in res:
                    res[k] += r[k]
        services.audit(s, who, "bank_import_api", "business", biz.id, **res)
        s.commit()
        return res

    @app.post("/api/v1/businesses/{bid}/bank/transactions")
    def bank_transactions(bid: int, body: TxBatchIn, s=Depends(session), who=Depends(actor)):
        """תנועות מאגרגטור / בנקאות פתוחה / סקריפט של המשרד — אותו מנגנון התאמה כמו קובץ."""
        return _import_tx_batch(s, services.get(s, Business, bid), body, who)

    @app.post("/api/v1/inbound/transactions/{token}")
    async def inbound_transactions(token: str, request: Request, s=Depends(session)):
        """webhook לשירותי חיבור בנק/אשראי. חתום ב-HMAC-SHA256 (X-CPA-Signature: sha256=...) עם
        CPA_AI_TX_WEBHOOK_SECRET; בלי סוד מוגדר הנקודה סגורה."""
        import hashlib
        import hmac
        secret = os.getenv("CPA_AI_TX_WEBHOOK_SECRET", "")
        if not secret:
            raise HTTPException(503, "קליטת תנועות לא הוגדרה (CPA_AI_TX_WEBHOOK_SECRET)")
        raw = await request.body()
        if len(raw) > 5 * 1024 * 1024:
            raise HTTPException(413, "גדול מדי")
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not secrets.compare_digest(request.headers.get("x-cpa-signature", ""), expected):
            raise HTTPException(403, "חתימה לא תקינה")
        biz = _biz_by_token(s, token)
        try:
            body = TxBatchIn.model_validate_json(raw)
        except Exception:  # noqa: BLE001
            raise HTTPException(422, "מבנה לא תקין")
        return _import_tx_batch(s, biz, body, "webhook:transactions")

    @app.get("/api/v1/businesses/{bid}/export/journal")
    def export_journal(bid: int, year: int = Query(ge=2015, le=2100), format: Literal["csv", "json"] = "csv",
                       s=Depends(session), who=Depends(actor)):
        from . import journal
        biz = services.get(s, Business, bid)
        rows = journal.journal_rows(s, biz.id, date(year, 1, 1), date(year, 12, 31))
        services.audit(s, who, "export_journal", "business", biz.id, year=year, lines=len(rows))
        s.commit()
        if format == "json":
            return {"year": year, "accounts": journal.ACCOUNTS, "lines": rows}
        return Response(journal.journal_csv(rows), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="journal_{biz.id}_{year}.csv"'})

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

    # --- עבודה משותפת רו"ח–לקוח: פניות, שרשור, כרטיס לקוח ---------------------------------
    from . import collab
    from .notify import Notifier
    app.state.notifier = Notifier.from_env(_wa_client)

    def _notify(s, th, p: Principal, text: str) -> None:
        """מודיע לצד שהכדור אצלו. ללקוח: WhatsApp/מייל של העסק. לרו"ח: CPA_AI_ACCOUNTANT_EMAIL."""
        biz = s.get(Business, th.business_id)
        if th.waiting_on == "client":
            kw = {"phone": biz.whatsapp_phone, "email": biz.contact_email}
        else:
            kw = {"phone": None, "email": os.getenv("CPA_AI_ACCOUNTANT_EMAIL")}
        msg = f"{biz.name} — {th.title}\n{text[:600]}\n\nלצפייה ומענה: {os.getenv('CPA_AI_PUBLIC_URL', '')}/#threads"
        try:
            sent = app.state.notifier.send(subject=f"CPA-AI: {th.title}", text=msg, **kw)
            services.audit(s, p.name or "anonymous", "notify", "thread", th.id, business_id=th.business_id,
                           channels=sent, to=th.waiting_on)
        except Exception as e:  # noqa: BLE001 — התראה לעולם לא מפילה את הפעולה
            services.audit(s, p.name or "anonymous", "notify_failed", "thread", th.id, error=type(e).__name__)
        s.commit()

    @app.get("/api/v1/businesses/{bid}/threads")
    def threads(bid: int, status: Optional[Literal["open", "closed"]] = None, s=Depends(session),
                who=Depends(actor)):
        services.get(s, Business, bid)
        return [collab.thread_out(s, t) for t in collab.list_threads(s, [bid], status)]

    @app.post("/api/v1/businesses/{bid}/threads", status_code=201)
    def create_thread(bid: int, body: ThreadIn, s=Depends(session), p: Principal = Depends(principal)):
        th = collab.create_thread(s, bid, kind=body.kind, title=body.title, body=body.body, author=p.name or "anonymous",
                                  role=p.role, document_id=body.document_id, due_date=body.due_date)
        _notify(s, th, p, body.body)
        return collab.thread_out(s, th)

    @app.post("/api/v1/businesses/{bid}/threads/request-receipts", status_code=201)
    def request_receipts(bid: int, s=Depends(session), p: Principal = Depends(principal)):
        th = collab.request_receipts(s, bid, p.name or "anonymous", p.role)
        if th is None:
            return Response(status_code=204)
        _notify(s, th, p, th.title)
        return collab.thread_out(s, th)

    @app.get("/api/v1/threads/{thread_id}")
    def get_thread(thread_id: int, s=Depends(session), who=Depends(actor)):
        from .db import Thread
        return collab.thread_out(s, services.get(s, Thread, thread_id), with_messages=True)

    @app.post("/api/v1/threads/{thread_id}/messages")
    def thread_reply(thread_id: int, body: ReplyIn, s=Depends(session), p: Principal = Depends(principal)):
        th = collab.reply(s, thread_id, body=body.body, author=p.name or "anonymous", role=p.role)
        _notify(s, th, p, body.body)
        return collab.thread_out(s, th)

    @app.post("/api/v1/threads/{thread_id}/upload", status_code=202)
    async def thread_upload(thread_id: int, files: list[UploadFile] = File(...), s=Depends(session),
                            p: Principal = Depends(principal)):
        from .db import Thread
        th = services.get(s, Thread, thread_id)
        biz = services.get(s, Business, th.business_id)
        out = await _ingest_files(s, biz, files, p.name or "anonymous")
        ids = out["documents"] + [d["of"] for d in out["duplicates"]]
        names = ", ".join(f"#{i}" for i in ids) or "—"
        th = collab.reply(s, thread_id, body=f"📎 צורפו מסמכים: {names}", author=p.name or "anonymous", role=p.role,
                          document_id=ids[0] if ids else None)
        _notify(s, th, p, f"צורפו מסמכים: {names}")
        return out

    @app.post("/api/v1/threads/{thread_id}/{action}")
    def thread_status(thread_id: int, action: Literal["close", "reopen"], s=Depends(session),
                      p: Principal = Depends(principal)):
        th = collab.set_status(s, thread_id, "closed" if action == "close" else "open", p.name or "anonymous", p.role)
        return collab.thread_out(s, th)

    @app.get("/api/v1/inbox")
    def inbox(s=Depends(session), p: Principal = Depends(principal)):
        """כל הפניות שמחכות לצד של המשתמש, בכל העסקים שמותרים לו."""
        ids = [b.id for b in s.scalars(select(Business)) if p.can_access(b.id)]
        side = collab.side_of(p.role)
        rows = collab.list_threads(s, ids, status="open", waiting_on=side) if ids else []
        names = {b.id: b.name for b in s.scalars(select(Business).where(Business.id.in_(ids)))} if ids else {}
        return {"side": side, "threads": [{**collab.thread_out(s, t), "business_name": names.get(t.business_id)}
                                          for t in rows]}

    @app.get("/api/v1/businesses/{bid}/profile")
    def get_profile(bid: int, s=Depends(session), p: Principal = Depends(principal)):
        biz = services.get(s, Business, bid)
        out = {"id": biz.id, "name": biz.name, "owner_name": biz.owner_name, "vat_id": biz.vat_id,
               "contact_email": biz.contact_email, "whatsapp_phone": biz.whatsapp_phone, "tags": biz.tags,
               "threads": collab.counts(s, biz.id)}
        if p.role != "client":
            out["internal_notes"] = biz.internal_notes
        return out

    @app.patch("/api/v1/businesses/{bid}/profile")
    def update_profile(bid: int, body: ProfileIn, s=Depends(session), p: Principal = Depends(principal)):
        biz = services.get(s, Business, bid)
        changes = body.model_dump(exclude_unset=True)
        if "internal_notes" in changes and p.role == "client":
            raise HTTPException(403, "הערות פנימיות — לרו\"ח בלבד")
        for k, v in changes.items():
            setattr(biz, k, v)
        services.audit(s, p.name or "anonymous", "profile_update", "business", biz.id, fields=sorted(changes))
        s.commit()
        return get_profile(bid, s, p)

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
        except storage.StorageError as e:
            raise HTTPException(503, f"לא ניתן לפענח את קובץ המקור: {e}")
        except (OSError, ValueError):
            raise HTTPException(404, "קובץ המקור לא נמצא באחסון")
        services.audit(s, who, "download_original", "document", doc.id)
        s.commit()
        name = quote(doc.attachment_name or f"document-{doc.id}")
        # רק סוגים שאומתו לפי תוכן בעת הקליטה. כל השאר מוגש כקובץ בינארי להורדה, לעולם לא כדף.
        safe = doc.attachment_mime if doc.attachment_mime in SAFE_SERVE_MIMES else "application/octet-stream"
        return Response(blob, media_type=safe, headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{name}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, no-store"})

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
                       who=Depends(actor), _signer=Depends(require_signer)):
        return rep_out(services.mark_submitted(s, rid, reference, who))

    # --- יועץ תכנון מס (לגיטימי בלבד; הפלט להכנה לשיחה עם מייצג) -------------------
    @app.get("/api/v1/businesses/{bid}/advisor")
    def advisor(bid: int, year: int = Query(ge=2015, le=2100), credit_points: float = Query(2.25, ge=0, le=20),
                existing_pension: float = Query(0, ge=0), existing_keren: float = Query(0, ge=0),
                material_holder: bool = False, projected_profit: Optional[float] = None,
                carried_loss: float = Query(0, ge=0), donations: float = Query(0, ge=0),
                s=Depends(session), who=Depends(actor)):
        from .advisor import advise
        biz = services.get(s, Business, bid)
        services.audit(s, who, "advisor_run", "business", biz.id, year=year)
        s.commit()
        return advise(s, biz, year, credit_points, existing_pension, existing_keren, material_holder,
                      projected_profit=projected_profit, carried_loss=carried_loss, donations=donations)

    @app.post("/api/v1/businesses/{bid}/advisor/optimize")
    def advisor_optimize(bid: int, body: OptimizeIn, s=Depends(session), who=Depends(actor)):
        """תכנון משולב: חלוקת התקציב בין המנופים, מיקום במדרגות, תזמון הכנסה בין שנים, ומילואים."""
        from datetime import date as _d

        from .advisor import optimizer, profit_basis
        from .reports import periodic
        biz = services.get(s, Business, bid)
        est = periodic.build_annual_estimate(s, biz, body.year, body.credit_points, body.donations_done,
                                             body.carried_loss)
        pb = profit_basis(est["business_profit"], body.year, _d.today(), body.projected_profit)
        profit = pb["profit"]
        plan = optimizer.optimize(profit, body.year, body.credit_points, body.budget,
                                  existing_pension=body.existing_pension, existing_keren=body.existing_keren,
                                  carried_loss=body.carried_loss, planned_donations=body.planned_donations,
                                  donations_done=body.donations_done)
        from .tax_model import estimate_burden
        taxable = estimate_burden(profit, body.year, body.credit_points, carried_loss=body.carried_loss)["taxable_income"]
        out = {
            "profit_basis": pb, "plan": plan, "bracket": optimizer.bracket_position(taxable, body.year),
            "timing": (optimizer.income_timing(profit, body.next_year_profit, body.year, body.credit_points)
                       if body.next_year_profit is not None else None),
            "reserve_duty": (optimizer.reserve_duty(body.reserve.planned_days, body.reserve.ni_monthly_base,
                                                    body.reserve.actual_monthly_income) if body.reserve else None),
            "disclaimer": ("תכנון מס לגיטימי בלבד, על בסיס פרמטרים שלא אומתו מול רשות המסים וביטוח לאומי. "
                           "להכנה לשיחה עם המייצג — לא ייעוץ מחייב. לא מוצעים: הסתרת הכנסה, הוצאות פיקטיביות, "
                           "דחיית חשבונית על עסקה שבוצעה, או דיווח הכנסה לא אמיתי לביטוח לאומי"),
        }
        services.audit(s, who, "advisor_optimize", "business", biz.id, year=body.year, budget=body.budget,
                       saving=plan["total_saving"])
        s.commit()
        return out

    @app.post("/api/v1/businesses/{bid}/advisor/what-if")
    def advisor_what_if(bid: int, body: WhatIfIn, s=Depends(session), who=Depends(actor)):
        from .advisor import what_if
        biz = services.get(s, Business, bid)
        services.audit(s, who, "what_if_run", "business", biz.id, year=body.year,
                       scenarios=[x.get("type") for x in body.scenarios])
        s.commit()
        return what_if(s, biz, body.year, body.credit_points, body.existing_pension, body.existing_keren,
                       body.carried_loss, body.donations, body.scenarios, body.projected_profit)

    # --- פרטיות וביקורת -----------------------------------------------------------
    @app.get("/api/v1/businesses/{bid}/privacy/export")
    def privacy_export(bid: int, s=Depends(session), who=Depends(actor)):
        return services.privacy_export(s, bid, who)

    @app.post("/api/v1/businesses/{bid}/privacy/purge-raw")
    def privacy_purge(bid: int, s=Depends(session), who=Depends(actor)):
        return {"purged_documents": services.privacy_purge_raw(s, bid, who)}

    @app.get("/api/v1/audit/verify")
    def audit_verify(s=Depends(session), who=Depends(actor), _admin=Depends(require_admin)):
        return services.verify_audit(s)

    @app.get("/api/v1/audit")
    def audit_log(limit: int = Query(100, le=1000), s=Depends(session), who=Depends(actor), _admin=Depends(require_admin)):
        rows = s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))
        return [{c.name: getattr(a, c.name) for c in AuditLog.__table__.columns} for a in rows]

    return app

