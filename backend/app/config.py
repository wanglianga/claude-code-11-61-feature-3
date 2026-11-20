"""Application configuration (environment driven, demo friendly)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    db_schema: str
    secret_key: str
    demo_mode: bool
    auto_seed: bool
    token_ttl_hours: int = 12


def load_settings() -> Settings:
    return Settings(
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev",
        ),
        db_schema=os.getenv("DB_SCHEMA", "drs"),
        secret_key=os.getenv("SECRET_KEY", "dr-settlement-demo-secret"),
        demo_mode=os.getenv("DEMO_MODE", "true").lower() in {"1", "true", "yes"},
        auto_seed=os.getenv("AUTO_SEED", "true").lower() in {"1", "true", "yes"},
    )


settings = load_settings()
