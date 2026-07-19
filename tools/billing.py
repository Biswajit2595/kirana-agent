"""
Billing skill. The core discipline:
  - draft bill + line items = pure bookkeeping, NO stock touched
  - finalize_bill = the ONLY place stock is decremented, atomically,
    with the conditional UPDATE that makes oversell structurally impossible
    (not "checked", impossible — see finalize_bill below)
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db import transaction, idempotent, InsufficientStock, NotFound, BelowCostSale
from gst import compute_line_gst, compute_bill_totals


def start_bill(conn, owner_id, customer_name=None):
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO bills (owner_id, status, customer_name) VALUES (?, 'draft', ?)",
            (owner_id, customer_name),
        )
        return {"bill_id": cur.lastrowid}


def add_bill_item(conn, bill_id, product_id, qty, confirm_below_cost=False):
    """Adds to the DRAFT only. Does not touch products.qty (the real,
    authoritative oversell guard is still finalize_bill's atomic UPDATE —
    this function's stock check is a courtesy early-warning, not the
    enforcement point, since stock can still move between add and finalize).

    Below-cost is actually BLOCKED here, not just flagged: the brief says
    "don't sell below cost — confirm or refuse", so a first call without
    confirm_below_cost=True is refused; the model must explicitly ask the
    owner and pass confirm_below_cost=True to override."""
    bill = conn.execute("SELECT status FROM bills WHERE id = ?", (bill_id,)).fetchone()
    if not bill:
        raise NotFound(f"bill {bill_id} not found")
    if bill["status"] != "draft":
        raise ValueError(f"bill {bill_id} is {bill['status']}, cannot add items")

    product = conn.execute(
        "SELECT id, name, sell_price, cost_price, gst_rate, hsn_code, qty FROM products WHERE id = ?",
        (product_id,),
    ).fetchone()
    if not product:
        raise NotFound(f"product {product_id} not found")

    below_cost = product["sell_price"] < product["cost_price"]
    if below_cost and not confirm_below_cost:
        raise BelowCostSale(product_id, product["sell_price"], product["cost_price"])

    gst = compute_line_gst(product["sell_price"], qty, product["gst_rate"])

    # Soft warning only — draft items don't reserve stock, so this can go
    # stale by finalize time (e.g. another bill sells the item first). That's
    # fine: finalize_bill's atomic check is what's actually authoritative.
    stock_warning = qty > product["qty"]

    with transaction(conn):
        conn.execute(
            """INSERT INTO bill_items
               (bill_id, product_id, product_name_snapshot, qty, unit_price,
                hsn_code, gst_rate, cgst_amt, sgst_amt, line_total)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (bill_id, product_id, product["name"], qty, product["sell_price"],
             product["hsn_code"], product["gst_rate"], gst["cgst_amt"], gst["sgst_amt"], gst["line_total"]),
        )

    return {
        "line_total": gst["line_total"],
        "available_stock": product["qty"],
        "stock_warning": stock_warning,   # True = "heads up, this may oversell at finalize time"
    }


def remove_bill_item(conn, bill_id, item_id):
    with transaction(conn):
        cur = conn.execute("DELETE FROM bill_items WHERE id = ? AND bill_id = ?", (item_id, bill_id))
        if cur.rowcount == 0:
            raise NotFound(f"item {item_id} not found on bill {bill_id}")
        return {"removed": item_id}


def edit_bill_item(conn, bill_id, item_id, new_qty):
    item = conn.execute(
        "SELECT product_id, unit_price, gst_rate FROM bill_items WHERE id = ? AND bill_id = ?",
        (item_id, bill_id),
    ).fetchone()
    if not item:
        raise NotFound(f"item {item_id} not found on bill {bill_id}")
    gst = compute_line_gst(item["unit_price"], new_qty, item["gst_rate"])
    with transaction(conn):
        conn.execute(
            "UPDATE bill_items SET qty=?, cgst_amt=?, sgst_amt=?, line_total=? WHERE id=?",
            (new_qty, gst["cgst_amt"], gst["sgst_amt"], gst["line_total"], item_id),
        )
        return {"item_id": item_id, "new_qty": new_qty, "line_total": gst["line_total"]}


def preview_bill(conn, bill_id):
    items = conn.execute(
        "SELECT id, product_id, product_name_snapshot, qty, unit_price, gst_rate, cgst_amt, sgst_amt, line_total "
        "FROM bill_items WHERE bill_id = ?", (bill_id,),
    ).fetchall()
    items = [dict(r) for r in items]
    totals = compute_bill_totals([
        {"base_amount": i["line_total"] - i["cgst_amt"] - i["sgst_amt"], **i} for i in items
    ]) if items else {"subtotal": 0, "cgst_total": 0, "sgst_total": 0, "grand_total": 0}
    return {"items": items, **totals}


@idempotent("finalize_bill")
def finalize_bill(conn, bill_id, payment_mode, payment_ref=None):
    """
    THE critical function. Everything the brief calls a 'hard part' converges
    here:
      - oversell guard: conditional UPDATE per line, all-or-nothing
      - idempotency: @idempotent decorator (retried finalize = replay, not re-run)
      - concurrency: BEGIN IMMEDIATE via the transaction() context manager
        gives this whole block a write lock before any row is touched
    """
    bill = conn.execute("SELECT status, owner_id FROM bills WHERE id = ?", (bill_id,)).fetchone()
    if not bill:
        raise NotFound(f"bill {bill_id} not found")
    if bill["status"] != "draft":
        raise ValueError(f"bill {bill_id} is already {bill['status']}")

    items = conn.execute(
        "SELECT id, product_id, qty, unit_price, gst_rate, cgst_amt, sgst_amt, line_total "
        "FROM bill_items WHERE bill_id = ?", (bill_id,),
    ).fetchall()
    if not items:
        raise ValueError("cannot finalize an empty bill")

    with transaction(conn):
        for item in items:
            # THE oversell guard: the check and the write are the SAME atomic
            # statement. No read-then-write gap for a second transaction to
            # race into. If rowcount is 0, either the product vanished or
            # (far more likely) there wasn't enough stock -- either way, refuse.
            cur = conn.execute(
                "UPDATE products SET qty = qty - ? WHERE id = ? AND qty >= ?",
                (item["qty"], item["product_id"], item["qty"]),
            )
            if cur.rowcount == 0:
                available = conn.execute(
                    "SELECT qty, name FROM products WHERE id = ?", (item["product_id"],)
                ).fetchone()
                raise InsufficientStock(
                    item["product_id"],
                    item["qty"],
                    available["qty"] if available else 0,
                )
            conn.execute(
                "INSERT INTO stock_ledger (product_id, delta_qty, reason, ref_type, ref_id) "
                "VALUES (?, ?, 'sale', 'bill', ?)",
                (item["product_id"], -item["qty"], bill_id),
            )

        totals = compute_bill_totals([
            {"base_amount": i["line_total"] - i["cgst_amt"] - i["sgst_amt"],
             "cgst_amt": i["cgst_amt"], "sgst_amt": i["sgst_amt"], "line_total": i["line_total"]}
            for i in items
        ])
        conn.execute(
            """UPDATE bills SET status='finalized', payment_mode=?, payment_ref=?,
               subtotal=?, cgst_total=?, sgst_total=?, grand_total=?, finalized_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (payment_mode, payment_ref, totals["subtotal"], totals["cgst_total"],
             totals["sgst_total"], totals["grand_total"], bill_id),
        )

    return {"bill_id": bill_id, "status": "finalized", **totals}


def cancel_bill(conn, bill_id):
    with transaction(conn):
        cur = conn.execute("UPDATE bills SET status='cancelled' WHERE id=? AND status='draft'", (bill_id,))
        if cur.rowcount == 0:
            raise ValueError("only draft bills can be cancelled")
        return {"bill_id": bill_id, "status": "cancelled"}
