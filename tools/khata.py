import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db import transaction, idempotent, NotFound


def _get_or_create_customer(conn, owner_id, name):
    row = conn.execute(
        "SELECT id FROM khata_customers WHERE owner_id=? AND name=?", (owner_id, name)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO khata_customers (owner_id, name) VALUES (?, ?)", (owner_id, name)
    )
    return cur.lastrowid


def _balance(conn, customer_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) as bal FROM khata_ledger WHERE customer_id=?",
        (customer_id,),
    ).fetchone()
    return row["bal"]


@idempotent("khata_add_credit")
def khata_add_credit(conn, owner_id, customer_name, amount, ref_bill_id=None):
    """Auto-creates the customer on first use -- this is the one place
    auto-create is safe, since putting money ON credit for a new name is
    an unambiguous, low-risk action. Contrast with khata_pay below."""
    with transaction(conn):
        customer_id = _get_or_create_customer(conn, owner_id, customer_name)
        conn.execute(
            "INSERT INTO khata_ledger (customer_id, type, amount, ref_bill_id) VALUES (?, 'credit_sale', ?, ?)",
            (customer_id, amount, ref_bill_id),
        )
        return {"customer": customer_name, "new_balance": _balance(conn, customer_id)}


@idempotent("khata_pay")
def khata_pay(conn, owner_id, customer_name, amount):
    """Does NOT auto-create -- settling a khata that doesn't exist is exactly
    the guardrail the brief calls out. Refuse, let the model relay it."""
    row = conn.execute(
        "SELECT id FROM khata_customers WHERE owner_id=? AND name=?", (owner_id, customer_name)
    ).fetchone()
    if not row:
        raise NotFound(f"no khata account for '{customer_name}'")
    with transaction(conn):
        conn.execute(
            "INSERT INTO khata_ledger (customer_id, type, amount) VALUES (?, 'payment', ?)",
            (row["id"], -amount),
        )
        return {"customer": customer_name, "new_balance": _balance(conn, row["id"])}


def khata_balance(conn, owner_id, customer_name):
    row = conn.execute(
        "SELECT id FROM khata_customers WHERE owner_id=? AND name=?", (owner_id, customer_name)
    ).fetchone()
    if not row:
        raise NotFound(f"no khata account for '{customer_name}'")
    return {"customer": customer_name, "balance": _balance(conn, row["id"])}
