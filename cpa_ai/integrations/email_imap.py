"""poller ל-IMAP: מושך הודעות שלא נקראו ומעביר אותן לצינור הקליטה.

ניתוב לעסק לפי plus-addressing בכתובת הנמען (inbox+TOKEN@gmail.com). הודעה בלי טוקן נכנסת לעסק
ברירת המחדל (CPA_AI_IMAP_DEFAULT_BUSINESS) אם הוגדר, אחרת מדולגת. אידמפוטנטיות לפי Message-ID,
כך שהרצה חוזרת אינה יוצרת כפילויות גם אם הסימון "נקרא" נכשל.

Gmail: דורש סיסמת אפליקציה (או OAuth, שאינו ממומש) והפעלת IMAP.
"""
from __future__ import annotations

import imaplib
import os
import time
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import select

from ..db import Business
from ..ingest import ingest
from ..services import audit
from .email_parser import parse_email, recipient_token


@dataclass
class ImapConfig:
    host: str
    user: str
    password: str
    folder: str = "INBOX"
    port: int = 993
    default_business: int | None = None

    @staticmethod
    def from_env() -> "ImapConfig | None":
        host = os.getenv("CPA_AI_IMAP_HOST")
        if not host:
            return None
        default = os.getenv("CPA_AI_IMAP_DEFAULT_BUSINESS")
        return ImapConfig(host=host, user=os.getenv("CPA_AI_IMAP_USER", ""), password=os.getenv("CPA_AI_IMAP_PASSWORD", ""),
                          folder=os.getenv("CPA_AI_IMAP_FOLDER", "INBOX"), port=int(os.getenv("CPA_AI_IMAP_PORT", "993")),
                          default_business=int(default) if default else None)


def poll_once(SessionLocal, cfg: ImapConfig, imap_factory: Callable = imaplib.IMAP4_SSL) -> dict:
    stats = {"fetched": 0, "documents": 0, "duplicates": 0, "skipped": 0, "unrouted": 0, "errors": 0}
    conn = imap_factory(cfg.host, cfg.port)
    try:
        conn.login(cfg.user, cfg.password)
        conn.select(cfg.folder)
        typ, data = conn.search(None, "UNSEEN")
        for num in (data[0].split() if data and data[0] else []):
            typ, fetched = conn.fetch(num, "(BODY.PEEK[])")
            raw = fetched[0][1]
            stats["fetched"] += 1
            with SessionLocal() as s:
                biz = None
                try:
                    item, msg = parse_email(raw)
                    token = recipient_token(msg)
                    biz = s.scalar(select(Business).where(Business.inbound_token == token)) if token else None
                    if biz is None and cfg.default_business:
                        biz = s.get(Business, cfg.default_business)
                    if biz is None:
                        stats["unrouted"] += 1
                        continue  # לא מסומן כנקרא — יטופל אחרי שיוגדר ניתוב
                    result = ingest(s, biz, item, f"email:{(item.sender or '')[:120]}")
                    stats["documents"] += len(result.documents)
                    stats["duplicates"] += len(result.duplicates)
                    stats["skipped"] += int(result.skipped)
                except Exception as e:  # noqa: BLE001 — הודעה אחת פגומה לא תעצור את כל התיבה
                    # מסומנת כנקראה כדי לא ליפול עליה בכל סבב; ההודעה נשארת בתיבה ונרשמת ביומן לטיפול ידני
                    s.rollback()
                    stats["errors"] += 1
                    audit(s, "imap", "inbound_failed", "business", biz.id if biz else None,
                          imap_uid=num.decode() if isinstance(num, bytes) else str(num),
                          error=f"{type(e).__name__}: {str(e)[:200]}")
                    s.commit()
            conn.store(num, "+FLAGS", "\\Seen")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return stats


def run_forever(SessionLocal, cfg: ImapConfig, interval: int = 120, imap_factory: Callable = imaplib.IMAP4_SSL,
                on_error: Callable[[Exception], None] | None = None) -> None:
    while True:
        try:
            poll_once(SessionLocal, cfg, imap_factory)
        except Exception as e:  # רשת/התחברות — ממשיכים לנסות
            if on_error:
                on_error(e)
        time.sleep(interval)
