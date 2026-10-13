from __future__ import annotations

import os
import secrets
from datetime import date, datetime, timezone

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint, create_engine)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Business(Base):
    __tablename__ = "businesses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    vat_id: Mapped[str] = mapped_column(String(9), unique=True)
    owner_name: Mapped[str] = mapped_column(String(200))
    vat_frequency: Mapped[str] = mapped_column(String(16), default="bimonthly")
    advance_rate_percent: Mapped[float] = mapped_column(Float, default=0.0)
    llm_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    # סוד קליטה: כתובת המייל הייעודית של העסק (webhook) — מזהה את העסק בלי הזדהות של המשתמש
    inbound_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True,
                                                      default=lambda: secrets.token_urlsafe(16))
    whatsapp_phone: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    direction: Mapped[str] = mapped_column(String(16))  # expense | income
    status: Mapped[str] = mapped_column(String(32))  # pending_review | confirmed | rejected
    extractor: Mapped[str] = mapped_column(String(32))
    doc_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    # תקופת הדיווח כשהיא שונה מתאריך המסמך: מסמך שהגיע אחרי שתקופתו אושרה משויך לתקופה הפתוחה הבאה
    period_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    # מספר עוסק של הספק/הלקוח (9 ספרות) — לחיפוש היסטוריה לפי צד: ספק חדש, חריגת סכום, חשבונית חוזרת
    party_id: Mapped[str | None] = mapped_column(String(9), nullable=True, index=True)
    # תשלום שהתקבל/שולם שלא דרך התאמת בנק (מזומן, צ'ק) — לגיול לקוחות
    paid_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    extracted: Mapped[dict] = mapped_column(JSON)
    decision: Mapped[dict] = mapped_column(JSON)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # מקור וקבצי מקור (חובת שמירת מסמכי מקור) ונתוני דה-דופליקציה
    source: Mapped[str] = mapped_column(String(16), default="manual", server_default="manual")
    source_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    attachment_path: Mapped[str | None] = mapped_column(String(300), nullable=True)
    attachment_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    attachment_mime: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InboundEvent(Base):
    """אידמפוטנטיות: webhook (במיוחד WhatsApp) נשלח מחדש כשאין תשובה מהירה — לא מעבדים פעמיים."""
    __tablename__ = "inbound_events"
    __table_args__ = (UniqueConstraint("source", "source_ref", name="uq_inbound_source_ref"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(16))
    source_ref: Mapped[str] = mapped_column(String(200))
    business_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VendorRule(Base):
    """זיכרון ספקים: סיווג שהבודק קבע ידנית חוזר אוטומטית בפעם הבאה מאותו ספק."""
    __tablename__ = "vendor_rules"
    __table_args__ = (UniqueConstraint("business_id", "vendor_id", name="uq_vendor_rule"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    vendor_id: Mapped[str] = mapped_column(String(9))
    vendor_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    category: Mapped[str] = mapped_column(String(40))
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BankTransaction(Base):
    __tablename__ = "bank_transactions"
    __table_args__ = (UniqueConstraint("business_id", "external_hash", name="uq_bank_hash"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    tx_date: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(String(300), default="")
    amount: Mapped[float] = mapped_column(Float)  # שלילי = חיוב
    matched_document_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open", server_default="open")  # open|matched|ignored
    external_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # vat_period | income_tax_advance | annual_estimate
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32), default="prepared")  # prepared | approved | superseded
    payload: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    approver_license_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approver_license_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # המערכת אינה מגישה לרשות המסים; כאן נרשם שהמייצג הגיש בפועל, עם מספר האישור שקיבל
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submission_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """יומן פעולות בלתי ניתן לעריכה דרך ה-API (תיקון 13 לחוק הגנת הפרטיות — תיעוד גישה ושינויים)."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64), default="GENESIS", server_default="GENESIS")
    row_hash: Mapped[str] = mapped_column(String(64), default="", server_default="")


def make_sessionmaker(url: str | None = None):
    url = url or os.getenv("CPA_AI_DATABASE_URL", "sqlite:///./cpa_ai.db")
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    from .migrate import migrate
    migrate(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
