"""Tiny database layer: Postgres (Neon/Supabase) in production, SQLite file for local dev.

Set DATABASE_URL=postgresql://... in .env to use Postgres. Without it, data goes to doubleaads.db.
Queries are written with `?` placeholders and converted for Postgres. IDs are generated in Python,
so the same SQL works on both.
"""
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
SQLITE_PATH = Path(__file__).resolve().parent.parent / "doubleaads.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    product TEXT NOT NULL,
    audience TEXT NOT NULL,
    platform TEXT NOT NULL,
    goal TEXT NOT NULL,
    daily_budget REAL NOT NULL,
    days INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_campaigns_user ON campaigns(user_id);
CREATE TABLE IF NOT EXISTS variants (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    label TEXT NOT NULL,
    headline TEXT NOT NULL,
    body TEXT NOT NULL,
    cta TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_variants_user ON variants(user_id);
CREATE TABLE IF NOT EXISTS results (
    id TEXT PRIMARY KEY,
    variant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    impressions INTEGER NOT NULL,
    clicks INTEGER NOT NULL,
    conversions INTEGER NOT NULL,
    spend REAL NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_user ON results(user_id);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


@contextmanager
def connect():
    if IS_PG:
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _sql(q: str) -> str:
    return q.replace("?", "%s") if IS_PG else q


def query(q: str, params: tuple = ()) -> list[dict]:
    with connect() as conn:
        cur = conn.execute(_sql(q), params)
        return [dict(r) for r in cur.fetchall()]


def execute(q: str, params: tuple = ()) -> None:
    with connect() as conn:
        conn.execute(_sql(q), params)


def init_db() -> None:
    with connect() as conn:
        for stmt in filter(str.strip, SCHEMA.split(";")):
            conn.execute(stmt)


init_db()
