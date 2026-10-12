"""זיהוי והרשאות: תפקידים, שיוך משתמש לעסקים, והגבלת קצב."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass

ROLES = ("admin", "accountant", "client")


@dataclass(frozen=True)
class Principal:
    name: str | None
    role: str = "admin"
    businesses: frozenset[int] | None = None  # None = כל העסקים

    def can_access(self, business_id: int) -> bool:
        return self.businesses is None or business_id in self.businesses

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_sign(self) -> bool:
        """אישור דוח והגשתו שמורים למייצג: רו"ח / יועץ מס / עו"ד (תפקיד accountant) או מנהל."""
        return self.role in ("admin", "accountant")

    def with_name(self, name: str) -> "Principal":
        return Principal(name, self.role, self.businesses)


def parse_users(raw: dict | None) -> dict[str, Principal]:
    """{מפתח: "שם"} (מנהל, תאימות לאחור) או {מפתח: {"name","role","businesses":[...]}}.
    אובייקט בלי role נחשב לקוח (הרשאה מינימלית). תפקיד לא מוכר נכשל מיד."""
    out: dict[str, Principal] = {}
    for key, v in (raw or {}).items():
        if v is None or isinstance(v, str):
            out[key] = Principal(v, "admin", None)
            continue
        role = v.get("role", "client")
        if role not in ROLES:
            raise ValueError(f"תפקיד לא מוכר בהגדרת משתמש: {role!r} (מותר: {', '.join(ROLES)})")
        biz = None if role == "admin" else frozenset(int(x) for x in (v.get("businesses") or []))
        out[key] = Principal(v.get("name"), role, biz)
    return out


class RateLimiter:
    """חלון הזזה בזיכרון, לפי מפתח. מתאים לתהליך בודד; מאחורי כמה תהליכים — להעביר ל-Redis או לפרוקסי."""

    def __init__(self, clock=time.monotonic):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._clock = clock

    def hit(self, key: str, limit: int, window: float) -> float | None:
        """רושם פגיעה. מחזיר None אם מותר, אחרת כמה שניות להמתין."""
        now = self._clock()
        if len(self._hits) > 5000:
            self._prune(now, window)
        q = self._hits[key]
        while q and now - q[0] >= window:
            q.popleft()
        if len(q) >= limit:
            return max(window - (now - q[0]), 0.1)
        q.append(now)
        return None

    def _prune(self, now: float, window: float) -> None:
        """מסיר דליים שכל הפגיעות בהם פגו — מונע גידול בלתי מוגבל בזיכרון."""
        for k in [k for k, q in self._hits.items() if not q or now - q[-1] >= window]:
            del self._hits[k]

    def peek(self, key: str, limit: int, window: float) -> float | None:
        """בודק בלי לרשום."""
        now = self._clock()
        q = self._hits[key]
        while q and now - q[0] >= window:
            q.popleft()
        return max(window - (now - q[0]), 0.1) if len(q) >= limit else None
