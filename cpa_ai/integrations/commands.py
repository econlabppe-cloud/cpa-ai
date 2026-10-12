"""פקודות טקסט קצרות בווטסאפ: סטטוס, מה חסר, עזרה."""
from __future__ import annotations

from ..home import build_home

HELP = ("אפשר לשלוח לי כאן תמונה, PDF או טקסט של חשבונית או קבלה, ואני רושם אותה.\n"
        "פקודות: *סטטוס* — איפה אנחנו החודש · *מה חסר* — מה מחכה לך · *עזרה*")


def _money(n: float) -> str:
    return f"₪{n:,.0f}"


def handle(s, biz, text: str) -> str | None:
    """מחזיר תשובה אם ההודעה היא פקודה, אחרת None (ואז מנסים לקרוא אותה כחשבונית)."""
    word = text.strip().strip("!.?").lower()
    if word in ("עזרה", "help", "תפריט", "?"):
        return HELP
    if word in ("סטטוס", "status", "מצב"):
        h = build_home(s, biz)
        m = h["month"]
        lines = [f"📊 החודש: הכנסות {_money(m['income'])} · הוצאות מוכרות {_money(m['expenses'])} · רווח {_money(m['profit'])}"]
        d = next((x for x in h["deadlines"] if x["status"] != "submitted"), h["deadlines"][-1])
        when = f"באיחור של {-d['days_left']} ימים" if d["days_left"] < 0 else f"עוד {d['days_left']} ימים"
        lines.append(f"🗓️ דוח מע\"מ: {when} (עד {d['due'][8:10]}/{d['due'][5:7]})")
        lines.append("✅ אין משהו שדורש אותך כרגע" if h["all_clear"] else f"👉 יש {sum(1 for t in h['tasks'] if t['priority'] <= 3)} דברים שמחכים לך — שלחו *מה חסר*")
        return "\n".join(lines)
    if word in ("מה חסר", "חסר", "משימות", "todo"):
        tasks = [t for t in build_home(s, biz)["tasks"] if t["priority"] <= 3]
        if not tasks:
            return "✅ הכל מסודר. אין משהו שחסר כרגע."
        return "מה מחכה לך:\n" + "\n".join(f"{i}. {t['title']} — {t['detail']}" for i, t in enumerate(tasks[:5], 1))
    return None
