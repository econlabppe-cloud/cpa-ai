from __future__ import annotations

import os
from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, create_engine
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    direction: Mapped[str] = mapped_column(String(16))  # expense | income
    status: Mapped[str] = mapped_column(String(32))  # pending_review | confirmed | rejected
    extractor: Mapped[str] = mapped_column(String(32))
    doc_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    extracted: Mapped[dict] = mapped_column(JSON)
    decision: Mapped[dict] = mapped_column(JSON)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
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
    prev_hash: Mapped[str] = mapped_column(String(64), default="GENESIS")
    row_hash: Mapped[str] = mapped_column(String(64), default="")


def make_sessionmaker(url: str | None = None):
    url = url or os.getenv("CPA_AI_DATABASE_URL", "sqlite:///./cpa_ai.db")
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
