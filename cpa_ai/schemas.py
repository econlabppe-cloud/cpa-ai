"""סכמות Pydantic.

הפרדה עקרונית: `ExtractedDocument` הוא כל מה שמודל השפה רשאי להחזיר — עובדות
מתוך המסמך בלבד. שיעורי הכרה, ניכוי מע"מ ונימוק משפטי נקבעים אך ורק במנוע
הכללים (`TaxDecision`).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

DocType = Literal["tax_invoice", "receipt", "tax_invoice_receipt", "credit_invoice", "unknown"]
LicenseType = Literal["cpa", "tax_advisor", "lawyer"]


class ExtractedDocument(BaseModel):
    doc_type: DocType = Field(description="סוג המסמך כפי שמופיע עליו")
    vendor_name: Optional[str] = Field(None, description="שם הספק")
    vendor_id: Optional[str] = Field(None, description="מספר עוסק מורשה / ח.פ כפי שמודפס, ספרות בלבד")
    doc_number: Optional[str] = Field(None, description="מספר המסמך")
    doc_date: Optional[date] = Field(None, description="תאריך המסמך")
    pre_vat_amount: Optional[float] = Field(None, description="סכום לפני מע\"מ כפי שמודפס")
    vat_amount: Optional[float] = Field(None, description="סכום המע\"מ כפי שמודפס")
    total_amount: Optional[float] = Field(None, description="סכום כולל לתשלום כפי שמודפס")
    allocation_number: Optional[str] = Field(None, description="מספר הקצאה (חשבוניות ישראל) אם מודפס")
    line_items: list[str] = Field(default_factory=list, description="תיאורי שורות הפריטים")
    category_hint: Optional[str] = Field(None, description="הצעה לקטגוריה; המנוע מחליט סופית")
    currency: str = Field("ILS", description="מטבע החשבונית: ILS / USD / EUR / GBP")
    payment_method: Literal["cash", "credit_card", "bank", "check", "unknown"] = Field(
        "unknown", description="אמצעי תשלום כפי שמופיע במסמך")
    is_foreign_vendor: bool = Field(False, description="ספק זר (ללא מספר עוסק ישראלי)")
    extraction_confidence: float = Field(0.0, ge=0, le=1, description="ביטחון בחילוץ השדות בלבד")


class TaxDecision(BaseModel):
    category: str
    category_label: str
    vat_rate: Optional[float]
    pre_vat_amount: Optional[float]
    vat_amount: Optional[float]
    total_amount: Optional[float]
    vendor_id_valid: bool
    currency: str = "ILS"
    payment_method: str = "unknown"
    is_foreign_vendor: bool = False
    income_tax_recognition: float
    recognized_expense: float
    vat_deduction_rate: float
    deductible_vat: float
    is_fixed_asset: bool
    asset_class: Optional[str] = None
    confidence: float
    needs_human_review: bool
    findings: list[dict]
    applied_rules: list[dict]


class BusinessIn(BaseModel):
    name: str
    vat_id: str = Field(description="מספר עוסק מורשה")
    owner_name: str
    vat_frequency: Literal["monthly", "bimonthly"] = "bimonthly"
    advance_rate_percent: float = Field(0.0, ge=0, le=100, description="שיעור מקדמות שקבע פקיד השומה")
    llm_consent: bool = Field(False, description="הסכמה מפורשת להעברת מסמכים לספק LLM חיצוני")


class BusinessOut(BusinessIn):
    id: int
    created_at: datetime


class ProcessTextIn(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)
    extractor: Literal["auto", "heuristic", "openai", "claude"] = "auto"


class IncomeIn(BaseModel):
    doc_type: DocType = "tax_invoice"
    doc_number: Optional[str] = None
    customer_name: Optional[str] = None
    customer_id: Optional[str] = None
    doc_date: date
    pre_vat_amount: float = Field(ge=0)
    vat_amount: Optional[float] = Field(None, ge=0)
    withholding_tax: float = Field(0.0, ge=0, description="ניכוי במקור שנוכה על ידי הלקוח")
    allocation_number: Optional[str] = None


class ReviewIn(BaseModel):
    action: Literal["confirm", "reject"]
    reviewer: str
    corrections: Optional[ExtractedDocument] = None
    category_override: Optional[str] = None
    note: Optional[str] = None


class ApprovalIn(BaseModel):
    approver_name: str
    license_type: LicenseType
    license_number: str = Field(min_length=3)
    attest: bool = Field(description="אישור שהדוח נבדק על ידי המאשר והוא נושא באחריות המקצועית")


class DocumentOut(BaseModel):
    id: int
    business_id: int
    direction: str
    status: str
    extractor: str
    extracted: dict
    decision: dict
    source: str = "manual"
    period_date: Optional[date] = None
    paid_on: Optional[date] = None
    attachment_name: Optional[str] = None
    created_at: datetime


class ReportOut(BaseModel):
    id: int
    business_id: int
    kind: str
    period_start: date
    period_end: date
    status: str
    payload: dict
    content_hash: str
    approved_by: Optional[str]
    approver_license_type: Optional[str]
    approver_license_number: Optional[str]
    approved_at: Optional[datetime]
    submitted_at: Optional[datetime] = None
    submission_ref: Optional[str] = None
    created_at: datetime


class ThreadIn(BaseModel):
    kind: Literal["document_request", "question", "task"]
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)
    document_id: Optional[int] = None
    due_date: Optional[date] = None


class ReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


class ProfileIn(BaseModel):
    contact_email: Optional[str] = Field(None, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    tags: Optional[str] = Field(None, max_length=200)
    internal_notes: Optional[str] = Field(None, max_length=5000)


class TxIn(BaseModel):
    date: date
    description: str = Field("", max_length=300)
    amount: float = Field(description="שלילי = חיוב, חיובי = זיכוי")
    account: Literal["bank", "card"] = "bank"
    external_id: Optional[str] = Field(None, max_length=100, description="מזהה יציב מהמקור — מונע כפילויות")


class TxBatchIn(BaseModel):
    transactions: list[TxIn] = Field(max_length=5000)


class MarkPaidIn(BaseModel):
    paid_on: Optional[date] = Field(None, description="תאריך התשלום; ריק = היום")
    unpaid: bool = Field(False, description="ביטול סימון תשלום")


class WhatIfIn(BaseModel):
    year: int = Field(ge=2015, le=2100)
    credit_points: float = Field(2.25, ge=0, le=20)
    existing_pension: float = Field(0.0, ge=0)
    existing_keren: float = Field(0.0, ge=0)
    carried_loss: float = Field(0.0, ge=0)
    donations: float = Field(0.0, ge=0)
    projected_profit: Optional[float] = Field(None, description="תחזית רווח שנתי; בלעדיה משתמשים ברווח עד היום")
    scenarios: list[dict] = Field(min_length=1, max_length=10)
