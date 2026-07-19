"""
DB layer. Two jobs only:
1. Give every mutating operation a real write-lock via BEGIN IMMEDIATE
   (this is what makes the oversell guard concurrency-safe, not just
   logically correct on paper).
2. Provide one idempotency decorator that every mutating tool wraps itself
   in, so retried Telegram updates replay instead of re-executing.
"""
import sqlite3
import json
import functools
from contextlib import contextmanager

import os
DB_PATH = os.environ.get("DB_PATH", "kirana.db")
# ^ On Railway: mount a volume, then set DB_PATH to a file inside it (e.g.
#   /app/data/kirana.db) via an env var, matching wherever you mounted the volume.
# On Fly.io: mount at /data (see fly.toml [mounts]), then set DB_PATH=/data/kirana.db.
# Left as the local default "kirana.db" for local dev / testing, where no
# persistent volume exists and none is needed.


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)  # autocommit; we manage txns explicitly
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(conn):
    """BEGIN IMMEDIATE grabs the write lock up front instead of failing
    optimistically later — this is the whole concurrency story for SQLite."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db():
    conn = get_conn()
    with open("schema.sql") as f:
        conn.executescript(f.read())
    conn.close()


class InsufficientStock(Exception):
    def __init__(self, product_id, requested, available):
        self.product_id = product_id
        self.requested = requested
        self.available = available
        super().__init__(f"product {product_id}: requested {requested}, available {available}")


class NotFound(Exception):
    pass


class BelowCostSale(Exception):
    def __init__(self, product_id, sell_price, cost_price):
        self.product_id = product_id
        self.sell_price = sell_price
        self.cost_price = cost_price
        super().__init__(f"product {product_id}: sell_price {sell_price} < cost_price {cost_price}")


def idempotent(tool_name):
    """Decorator for mutating tools. Wrapped function must accept
    idempotency_key as a kwarg and return a JSON-serializable dict.

    Retried call with the same key -> cached result is replayed, the
    function body never runs twice. This is the entire fix for Telegram's
    at-least-once delivery.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, idempotency_key=None, **kwargs):
            conn = kwargs.get("conn") or (args[0] if args else None)
            if idempotency_key is None:
                return fn(*args, **kwargs)

            existing = conn.execute(
                "SELECT result_json FROM idempotency_log WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                return json.loads(existing["result_json"])

            # fn manages its OWN transaction internally (it needs to, since the
            # oversell-guard UPDATE and the ledger insert must be atomic
            # together). We do NOT open a second transaction here -- SQLite
            # doesn't nest them. We log the idempotency result right after
            # fn's commit completes, in its own small transaction.
            result = fn(*args, **kwargs)
            with transaction(conn):
                conn.execute(
                    "INSERT INTO idempotency_log (idempotency_key, tool_name, result_json) VALUES (?, ?, ?)",
                    (idempotency_key, tool_name, json.dumps(result)),
                )
            return result
        return wrapper
    return decorator
