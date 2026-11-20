"""Database engine, session and schema helpers."""
from __future__ import annotations

import re

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", settings.db_schema):
    raise ValueError(f"invalid DB schema name: {settings.db_schema}")

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

metadata = MetaData(schema=settings.db_schema)


class Base(DeclarativeBase):
    metadata = metadata


def init_db() -> None:
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{settings.db_schema}"'))
    Base.metadata.create_all(engine)


def drop_schema() -> None:
    """Drop the schema with a bounded lock wait so a stray idle transaction cannot hang a demo reset."""
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '15s'"))
        conn.execute(text("SET LOCAL statement_timeout = '120s'"))
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{settings.db_schema}" CASCADE'))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
