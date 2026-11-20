"""Test fixtures: isolated schema per test session."""
from __future__ import annotations

import os

# Point tests at an isolated schema so the dev/demo data is never touched.
os.environ.setdefault("DB_SCHEMA", "drs_test")
os.environ.setdefault("AUTO_SEED", "true")

import pytest  # noqa: E402

from app.db import SessionLocal, drop_schema, init_db  # noqa: E402
from app.seed import seed, wait_for_db  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def prepared_database():
    wait_for_db()
    drop_schema()
    init_db()
    with SessionLocal() as db:
        seed(db)
    yield
    drop_schema()


@pytest.fixture()
def db():
    with SessionLocal() as session:
        yield session
