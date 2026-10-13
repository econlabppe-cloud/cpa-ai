"""התראות יוצאות ללקוח ולרו"ח: WhatsApp ומייל (SMTP). תמיד "במאמץ מיטבי" — כשל לא מפיל פעולה.

מגבלה: Meta מאפשרת הודעת WhatsApp חופשית רק בתוך 24 שעות מההודעה האחרונה של הלקוח. מחוץ לחלון
נדרשת תבנית מאושרת (template) — לא ממומש; ההודעה תיכשל ותירשם ביומן, והבקשה עדיין מופיעה במסך הבית.
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from typing import Callable


class Notifier:
    def __init__(self, wa_client_factory: Callable | None = None, phone_number_id: str | None = None,
                 smtp: dict | None = None):
        self._wa = wa_client_factory
        self._phone_number_id = phone_number_id
        self._smtp = smtp

    @classmethod
    def from_env(cls, wa_client_factory: Callable | None = None) -> "Notifier":
        smtp = None
        if os.getenv("CPA_AI_SMTP_HOST"):
            smtp = {"host": os.getenv("CPA_AI_SMTP_HOST"), "port": int(os.getenv("CPA_AI_SMTP_PORT", "587")),
                    "user": os.getenv("CPA_AI_SMTP_USER", ""), "password": os.getenv("CPA_AI_SMTP_PASSWORD", ""),
                    "sender": os.getenv("CPA_AI_SMTP_FROM", os.getenv("CPA_AI_SMTP_USER", ""))}
        return cls(wa_client_factory, os.getenv("CPA_AI_WHATSAPP_PHONE_NUMBER_ID"), smtp)

    def channels(self) -> dict:
        return {"whatsapp": bool(self._wa and self._phone_number_id), "email": bool(self._smtp)}

    def send(self, *, phone: str | None, email: str | None, subject: str, text: str) -> list[str]:
        """מחזיר את הערוצים שנשלח בהם. חריגה של ערוץ אחד לא עוצרת את השני."""
        sent, errors = [], []
        if phone and self._wa and self._phone_number_id:
            try:
                client = self._wa()
                if client is not None:
                    client.send_text(self._phone_number_id, phone, text)
                    sent.append("whatsapp")
            except Exception as e:  # noqa: BLE001
                errors.append(f"whatsapp: {type(e).__name__}")
        if email and self._smtp:
            try:
                msg = EmailMessage()
                msg["From"], msg["To"], msg["Subject"] = self._smtp["sender"], email, subject
                msg.set_content(text)
                with smtplib.SMTP(self._smtp["host"], self._smtp["port"], timeout=20) as srv:
                    srv.starttls()
                    if self._smtp["user"]:
                        srv.login(self._smtp["user"], self._smtp["password"])
                    srv.send_message(msg)
                sent.append("email")
            except Exception as e:  # noqa: BLE001
                errors.append(f"email: {type(e).__name__}")
        self.last_errors = errors
        return sent
