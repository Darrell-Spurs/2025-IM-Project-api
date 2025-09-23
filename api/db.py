import os
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from fastapi import Depends
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv(), override=False)  # dev-only; prod should use real env

# ---- Config ----
# Put your PG URL in an env var; don't hardcode secrets.
PG_URL = os.getenv("PG_URL")  # e.g. "postgresql://user:pass@host:port/db"
if not PG_URL:
    raise RuntimeError("Please set PG_URL environment variable.")

# Optional: require SSL on pooled connections (works for Supabase)
DB_POOL = SimpleConnectionPool(
    1, 10,
    os.getenv("PG_URL"),          # or dsn=os.getenv("PG_URL")
    sslmode="require",            # <-- pass directly, not in a dict
)

# ---- Helpers ----
def db_conn():
    """Checkout a DB connection from the pool."""
    conn = DB_POOL.getconn()
    conn.autocommit = False
    try:
        yield conn
    finally:
        try:
            conn.rollback()
        finally:
            DB_POOL.putconn(conn)