"""
Database connection pool for the Flask application.

Reads config/db_config.json and provides a ThreadedConnectionPool
so connections are reused across requests rather than opened per-call.
"""

import json
import os
import logging
import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

_pool: pg_pool.ThreadedConnectionPool | None = None


def _config_path():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(root, "config", "db_config.json")


def _load_config():
    path = _config_path()
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def init_pool(minconn: int = 1, maxconn: int = 10):
    """Initialise the connection pool. Call once at app startup."""
    global _pool
    if _pool is not None:
        return
    cfg = _load_config()
    _pool = pg_pool.ThreadedConnectionPool(
        minconn,
        maxconn,
        host=cfg.get("host", "localhost"),
        port=int(cfg.get("port", 5432)),
        dbname=cfg.get("dbname", "drone_progress"),
        user=cfg.get("user", "admin"),
        password=cfg.get("password", ""),
    )
    logger.info("DB connection pool initialised (min=%d, max=%d)", minconn, maxconn)


def get_db():
    """Borrow a connection from the pool."""
    if _pool is None:
        init_pool()
    return _pool.getconn()


def release_db(conn):
    """Return a connection to the pool."""
    if _pool and conn:
        _pool.putconn(conn)


def close_pool():
    """Close all connections. Call on app teardown."""
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None


def query(sql: str, params=None) -> list[dict]:
    """Execute a SELECT and return all rows as a list of dicts."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]
    except Exception:
        conn.rollback()
        logger.exception("DB query error: %s", sql[:120])
        raise
    finally:
        release_db(conn)


def query_one(sql: str, params=None) -> dict | None:
    """Execute a SELECT and return the first row as a dict, or None."""
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params=None):
    """Execute a non-SELECT statement (INSERT / UPDATE / DELETE). Auto-commits."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("DB execute error: %s", sql[:120])
        raise
    finally:
        release_db(conn)


def execute_returning(sql: str, params=None):
    """Execute INSERT/UPDATE with RETURNING and return the first row as a dict."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    except Exception:
        conn.rollback()
        logger.exception("DB execute_returning error: %s", sql[:120])
        raise
    finally:
        release_db(conn)


def execute_transaction(statements: list[tuple]):
    """Execute multiple (sql, params) tuples in a single transaction."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            for sql, params in statements:
                cur.execute(sql, params)
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("DB transaction error")
        raise
    finally:
        release_db(conn)
