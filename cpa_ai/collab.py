"""עבודה משותפת רו"ח–לקוח: בקשות מסמכים, שאלות על מסמך, משימות, ושרשור הודעות.

עיקרון: כל פנייה היא "שרשור" עם `waiting_on` — אצל מי הכדור. כל תגובה מעבירה את הכדור לצד השני,
וכך מסך הבית של כל צד מראה רק מה שמחכה לו. מסמך שמגיע מהלקוח (העלאה, מייל, WhatsApp) כשיש
בקשת מסמכים פתוחה מצורף אליה אוטומטית; בקשת קבלות לחיובי בנק נסגרת לבד כשכל החיובים הותאמו.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import BankTransaction, Business, Document, Thread, ThreadMessage, utcnow
from .services import Conflict, NotFound, audit, get

KINDS = {"document_request": "בקשת מסמכים", "question": "שאלה", "task": "משימה"}


def side_of(role: str) -> str:
    return "client" if role == "client" else "accountant"


def other(side: str) -> str:
    return "accountant" if side == "client" else "client"


def _message(s: Session, th: Thread, author: str, role: str, body: str, document_id: int | None = None) -> ThreadMessage:
    m = ThreadMessage(thread_id=th.id, author=author, role=role, body=body[:5000], document_id=document_id)
    s.add(m)
    th.updated_at = utcnow()
    return m


def create_thread(s: Session, business_id: int, *, kind: str, title: str, body: str, author: str, role: str,
                  document_id: int | None = None, due_date: date | None = None, refs: dict | None = None) -> Thread:
    if kind not in KINDS:
        raise ValueError(f"סוג פנייה לא מוכר: {kind}")
    get(s, Business, business_id)
    if document_id is not None:
        doc = s.get(Document, document_id)
        if doc is None or doc.business_id != business_id:  # לא מצרפים מסמך של עסק אחר
            raise NotFound("המסמך לא נמצא")
    side = side_of(role)
    th = Thread(business_id=business_id, kind=kind, title=title.strip()[:200], waiting_on=other(side),
                document_id=document_id, due_date=due_date, refs=refs or {}, created_by=author, created_role=side)
    s.add(th)
    s.flush()
    _message(s, th, author, side, body)
    audit(s, author, "thread_create", "thread", th.id, business_id=business_id, kind=kind, waiting_on=th.waiting_on)
    s.commit()
    return th


def reply(s: Session, thread_id: int, *, body: str, author: str, role: str, document_id: int | None = None) -> Thread:
    th = get(s, Thread, thread_id)
    side = side_of(role)
    if th.status == "closed":
        th.status = "open"  # תגובה על פנייה סגורה פותחת אותה מחדש
    _message(s, th, author, side, body, document_id)
    th.waiting_on = other(side)
    audit(s, author, "thread_reply", "thread", th.id, business_id=th.business_id, waiting_on=th.waiting_on)
    s.commit()
    return th


def set_status(s: Session, thread_id: int, status: str, author: str, role: str) -> Thread:
    th = get(s, Thread, thread_id)
    if status not in ("open", "closed"):
        raise ValueError("סטטוס לא תקין")
    if th.status == status:
        raise Conflict("הפנייה כבר במצב הזה")
    th.status = status
    _message(s, th, author, "system", "הפנייה נסגרה" if status == "closed" else "הפנייה נפתחה מחדש")
    audit(s, author, f"thread_{status}", "thread", th.id, business_id=th.business_id)
    s.commit()
    return th


def thread_out(s: Session, th: Thread, with_messages: bool = False) -> dict:
    out = {"id": th.id, "business_id": th.business_id, "kind": th.kind, "kind_label": KINDS[th.kind],
           "title": th.title, "status": th.status, "waiting_on": th.waiting_on, "document_id": th.document_id,
           "refs": th.refs or {}, "due_date": th.due_date.isoformat() if th.due_date else None,
           "overdue": bool(th.due_date and th.status == "open" and th.due_date < date.today()),
           "created_by": th.created_by, "created_at": th.created_at.isoformat(), "updated_at": th.updated_at.isoformat()}
    msgs = list(s.scalars(select(ThreadMessage).where(ThreadMessage.thread_id == th.id).order_by(ThreadMessage.id)))
    out["messages_count"] = len(msgs)
    out["last_message"] = msgs[-1].body[:160] if msgs else ""
    if with_messages:
        out["messages"] = [{"id": m.id, "author": m.author, "role": m.role, "body": m.body,
                            "document_id": m.document_id, "created_at": m.created_at.isoformat()} for m in msgs]
    return out


def list_threads(s: Session, business_ids: list[int], status: str | None = None,
                 waiting_on: str | None = None) -> list[Thread]:
    q = select(Thread).where(Thread.business_id.in_(business_ids))
    if status:
        q = q.where(Thread.status == status)
    if waiting_on:
        q = q.where(Thread.waiting_on == waiting_on)
    return list(s.scalars(q.order_by(Thread.updated_at.desc())))


def request_receipts(s: Session, business_id: int, author: str, role: str) -> Thread | None:
    """בקשת קבלות אחת לכל חיובי הבנק/האשראי שאין להם מסמך. None אם אין מה לבקש."""
    from . import banking
    open_tx = banking.unmatched(s, business_id)["transactions_without_document"]
    already = set()
    for th in list_threads(s, [business_id], status="open"):
        already.update(th.refs.get("bank_tx_ids", []))
    todo = [t for t in open_tx if t["id"] not in already]
    if not todo:
        return None
    lines = [f"• {t['date']} — {t['description']} — ₪{abs(t['amount']):,.2f}" for t in todo[:40]]
    more = f"\n…ועוד {len(todo) - 40}" if len(todo) > 40 else ""
    body = ("חסרות קבלות/חשבוניות לחיובים הבאים. אפשר להעלות כאן, לשלוח במייל או בווטסאפ — "
            "המערכת תתאים אותן לבד. חיוב פרטי? כתבו לנו ונסמן אותו.\n" + "\n".join(lines) + more)
    return create_thread(s, business_id, kind="document_request", title=f"חסרות {len(todo)} קבלות לחיובים",
                         body=body, author=author, role=role, refs={"bank_tx_ids": [t["id"] for t in todo]})


def on_documents(s: Session, biz: Business, docs: list[Document], source: str, actor: str,
                 attach: bool = True) -> list[int]:
    """מסמכים חדשים: בקשת קבלות שכל חיוביה הותאמו נסגרת; ואם `attach` (המסמך בא מהלקוח) —
    הם מצורפים לבקשת המסמכים הפתוחה הוותיקה. מחזיר את מזהי השרשורים שעודכנו. בלי commit."""
    if not docs:
        return []
    from . import banking
    banking.auto_match(s, biz.id)
    touched = []
    requests = list(s.scalars(select(Thread).where(
        Thread.business_id == biz.id, Thread.kind == "document_request", Thread.status == "open",
        Thread.waiting_on == "client").order_by(Thread.created_at)))
    for th in requests:
        tx_ids = th.refs.get("bank_tx_ids") or []
        if tx_ids:
            txs = [s.get(BankTransaction, i) for i in tx_ids]
            remaining = [t for t in txs if t is not None and t.status == "open"]
            if not remaining:
                _message(s, th, "מערכת", "system", "כל החיובים בבקשה הותאמו למסמכים — הבקשה נסגרה אוטומטית")
                th.status = "closed"
                touched.append(th.id)
            continue
        if attach and th.id not in touched:
            for d in docs:
                _message(s, th, actor, "client", f"📎 התקבל מסמך #{d.id} ({source})", d.id)
            th.waiting_on = "accountant"
            touched.append(th.id)
            break  # מצרפים לבקשה אחת בלבד, הוותיקה ביותר
    for tid in touched:
        audit(s, actor, "thread_auto_update", "thread", tid, business_id=biz.id, source=source)
    return touched


def counts(s: Session, business_id: int) -> dict:
    open_ = list_threads(s, [business_id], status="open")
    return {"waiting_on_client": sum(t.waiting_on == "client" for t in open_),
            "waiting_on_accountant": sum(t.waiting_on == "accountant" for t in open_),
            "overdue": sum(1 for t in open_ if t.due_date and t.due_date < date.today() and t.waiting_on == "client")}
