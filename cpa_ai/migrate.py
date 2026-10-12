"""מיגרציה קלה: מוסיפה עמודות חסרות לטבלאות קיימות (SQLite ו-PostgreSQL).

אינה מחליפה כלי מיגרציות מלא (Alembic) — היא מכסה רק הוספת עמודות, שזה מה שמאפשר לעדכן
מסד קיים בלי לאבד מסמכים. שינוי סוג עמודה, שינוי שם או מחיקה עדיין דורשים כלי ייעודי.
"""
from __future__ import annotations

import secrets

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .db import Base, Business


def _default_sql(col) -> str:
    sd = col.server_default
    if sd is None:
        return ""
    arg = getattr(sd, "arg", None)
    return f" DEFAULT '{arg}'" if arg is not None else ""


def ensure_columns(engine: Engine) -> list[str]:
    added: list[str] = []
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl_type = col.type.compile(dialect=engine.dialect)
                default = _default_sql(col)
                # NOT NULL רק כשיש ברירת מחדל בשרת; אחרת העמודה החדשה nullable
                not_null = " NOT NULL" if (default and not col.nullable) else ""
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl_type}{default}{not_null}'))
                added.append(f"{table.name}.{col.name}")
    return added


def backfill_inbound_tokens(engine: Engine) -> int:
    from sqlalchemy.orm import Session
    n = 0
    with Session(engine) as s:
        for biz in s.query(Business).filter(Business.inbound_token.is_(None)):
            biz.inbound_token = secrets.token_urlsafe(16)
            n += 1
        s.commit()
    return n


def migrate(engine: Engine) -> dict:
    added = ensure_columns(engine)
    return {"added_columns": added, "tokens_backfilled": backfill_inbound_tokens(engine)}
