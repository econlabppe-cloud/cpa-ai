from __future__ import annotations

import os


def inbound_address(biz) -> str | None:
    """כתובת המייל שאליה הלקוח מעביר חשבוניות. נבנית מדומיין ייעודי או מתיבת ה-IMAP (plus-addressing)."""
    token = biz.inbound_token
    if not token:
        return None
    domain = os.getenv("CPA_AI_INBOUND_EMAIL_DOMAIN")
    if domain:
        return f"inbox+{token}@{domain}"
    user = os.getenv("CPA_AI_IMAP_USER", "")
    if "@" in user:
        local, dom = user.split("@", 1)
        return f"{local}+{token}@{dom}"
    return None
