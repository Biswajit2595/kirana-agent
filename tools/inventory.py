"""
Inventory skill. Every function here is deliberately dumb: it does exactly
what it's told, validated against the DB, no interpretation of intent.
Disambiguation ("which atta?") is the model's job via search_product,
never a regex/keyword match here.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db import transaction, idempotent, NotFound


def add_product(conn, owner_id, name, unit, gst_rate, cost_price, sell_price,
                 brand=None, is_loose=False, hsn_code=None, reorder_level=0):
    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO products
               (owner_id, name, brand, unit, is_loose, hsn_code, gst_rate,
                cost_price, sell_price, qty, reorder_level)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (owner_id, name, brand, unit, int(is_loose), hsn_code, gst_rate,
             cost_price, sell_price, reorder_level),
        )
        return {"product_id": cur.lastrowid, "name": name}


def search_product(conn, owner_id, query, limit=5):
    """Fuzzy-ish match by substring on name/brand. Returns candidates for
    the MODEL to disambiguate with the owner — this function never guesses."""
    like = f"%{query}%"
    rows = conn.execute(
        """SELECT id, name, brand, unit, qty, sell_price, is_loose
           FROM products WHERE owner_id = ? AND (name LIKE ? OR brand LIKE ?)
           ORDER BY name LIMIT ?""",
        (owner_id, like, like, limit),
    ).fetchall()
    return {"matches": [dict(r) for r in rows]}


def check_stock(conn, product_id):
    row = conn.execute(
        "SELECT id, name, qty, unit, reorder_level FROM products WHERE id = ?",
        (product_id,),
    ).fetchone()
    if not row:
        raise NotFound(f"product {product_id} not found")
    return dict(row)


def low_stock_report(conn, owner_id):
    rows = conn.execute(
        """SELECT id, name, qty, unit, reorder_level FROM products
           WHERE owner_id = ? AND qty <= reorder_level ORDER BY qty ASC""",
        (owner_id,),
    ).fetchall()
    return {"low_stock": [dict(r) for r in rows]}


@idempotent("receive_stock")
def receive_stock(conn, product_id, qty, new_cost_price=None):
    """Stock-in. Updates cached qty AND appends to ledger in one transaction —
    this pairing (cache + ledger, always together, always atomic) is the
    pattern to copy for every future stock mutation."""
    with transaction(conn):
        row = conn.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone()
        if not row:
            raise NotFound(f"product {product_id} not found")

        if new_cost_price is not None:
            conn.execute("UPDATE products SET cost_price = ? WHERE id = ?", (new_cost_price, product_id))

        conn.execute("UPDATE products SET qty = qty + ? WHERE id = ?", (qty, product_id))
        conn.execute(
            "INSERT INTO stock_ledger (product_id, delta_qty, reason) VALUES (?, ?, 'receive')",
            (product_id, qty),
        )
        new_qty = conn.execute("SELECT qty FROM products WHERE id = ?", (product_id,)).fetchone()["qty"]
        return {"product_id": product_id, "new_qty": new_qty}


@idempotent("adjust_stock")
def adjust_stock(conn, product_id, delta, reason):
    """Manual correction only. There is deliberately NO delete_product tool
    anywhere in this codebase — corrections go through here, signed and
    logged, never silently erased."""
    if not reason or not reason.strip():
        raise ValueError("adjust_stock requires a non-empty reason")
    with transaction(conn):
        cur = conn.execute(
            "UPDATE products SET qty = qty + ? WHERE id = ? AND qty + ? >= 0",
            (delta, product_id, delta),
        )
        if cur.rowcount == 0:
            raise ValueError("adjustment would take stock negative — refused")
        conn.execute(
            "INSERT INTO stock_ledger (product_id, delta_qty, reason) VALUES (?, ?, 'adjustment')",
            (product_id, delta),
        )
        new_qty = conn.execute("SELECT qty FROM products WHERE id = ?", (product_id,)).fetchone()["qty"]
        return {"product_id": product_id, "new_qty": new_qty, "reason": reason}
