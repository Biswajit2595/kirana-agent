import sys, os, threading, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import db as dbmod


def fresh_db():
    """Isolated DB file per test so tests don't stomp on each other."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    dbmod.DB_PATH = path
    dbmod.init_db()
    return path


def setup_owner_and_product(conn, qty=6):
    conn.execute("INSERT INTO owners (telegram_chat_id, shop_name) VALUES ('t1','Test Store')")
    owner_id = conn.execute("SELECT id FROM owners").fetchone()["id"]
    conn.execute(
        """INSERT INTO products (owner_id, name, unit, gst_rate, cost_price, sell_price, qty, reorder_level)
           VALUES (?, 'Maggi 70g', 'packet', 12, 10, 14, ?, 5)""",
        (owner_id, qty),
    )
    product_id = conn.execute("SELECT id FROM products").fetchone()["id"]
    return owner_id, product_id


def test_oversell_is_refused_at_tool_layer():
    from tools import billing
    fresh_db()
    conn = dbmod.get_conn()
    owner_id, product_id = setup_owner_and_product(conn, qty=6)

    bill = billing.start_bill(conn, owner_id)
    billing.add_bill_item(conn, bill["bill_id"], product_id, qty=10)  # asking for more than exists

    try:
        billing.finalize_bill(conn, bill["bill_id"], payment_mode="cash", idempotency_key="k1")
        assert False, "should have raised InsufficientStock"
    except dbmod.InsufficientStock as e:
        assert e.available == 6
        assert e.requested == 10

    # stock must be UNCHANGED — the whole bill rolled back, not partially applied
    remaining = conn.execute("SELECT qty FROM products WHERE id=?", (product_id,)).fetchone()["qty"]
    assert remaining == 6, f"expected stock untouched at 6, got {remaining}"


def test_retried_finalize_does_not_double_decrement():
    """Simulates Telegram redelivering the same update_id -> same idempotency_key."""
    from tools import billing
    fresh_db()
    conn = dbmod.get_conn()
    owner_id, product_id = setup_owner_and_product(conn, qty=6)

    bill = billing.start_bill(conn, owner_id)
    billing.add_bill_item(conn, bill["bill_id"], product_id, qty=2)

    key = "update-42:finalize_bill"
    r1 = billing.finalize_bill(conn, bill["bill_id"], payment_mode="upi", idempotency_key=key)
    r2 = billing.finalize_bill(conn, bill["bill_id"], payment_mode="upi", idempotency_key=key)  # retry

    assert r1 == r2, "retried call must return the identical cached result"
    remaining = conn.execute("SELECT qty FROM products WHERE id=?", (product_id,)).fetchone()["qty"]
    assert remaining == 4, f"expected 6-2=4 decremented exactly once, got {remaining}"


def test_two_concurrent_bills_racing_last_unit_only_one_wins():
    """The real concurrency test: two threads finalize bills for the SAME
    product with only 1 unit in stock, at the same time. Exactly one must
    succeed, the other must be refused -- never both, never neither."""
    from tools import billing
    fresh_db()
    setup_conn = dbmod.get_conn()
    owner_id, product_id = setup_owner_and_product(setup_conn, qty=1)  # only 1 in stock
    setup_conn.close()

    results = {}

    def attempt(name):
        conn = dbmod.get_conn()  # each thread gets its own connection, as it would in a real server
        try:
            bill = billing.start_bill(conn, owner_id)
            billing.add_bill_item(conn, bill["bill_id"], product_id, qty=1)
            billing.finalize_bill(conn, bill["bill_id"], payment_mode="cash", idempotency_key=f"key-{name}")
            results[name] = "success"
        except dbmod.InsufficientStock:
            results[name] = "refused"
        finally:
            conn.close()

    t1 = threading.Thread(target=attempt, args=("A",))
    t2 = threading.Thread(target=attempt, args=("B",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    outcomes = list(results.values())
    assert outcomes.count("success") == 1, f"expected exactly 1 success, got {outcomes}"
    assert outcomes.count("refused") == 1, f"expected exactly 1 refusal, got {outcomes}"

    final_conn = dbmod.get_conn()
    final_qty = final_conn.execute("SELECT qty FROM products WHERE id=?", (product_id,)).fetchone()["qty"]
    assert final_qty == 0, f"stock must be exactly 0 after one sale of the only unit, got {final_qty}"


def test_stock_in_racing_a_sale_never_corrupts_quantity():
    """Brief explicitly names this case: 'a sale plus a stock-in in flight
    at once must not corrupt stock.' Start at 5, race a receive_stock(+10)
    against a finalize_bill(-3) for the SAME product. Regardless of which
    runs first, the final quantity must be exactly 5 + 10 - 3 = 12 -- never
    a lost update from the two writes stomping on each other."""
    from tools import billing, inventory
    fresh_db()
    setup_conn = dbmod.get_conn()
    owner_id, product_id = setup_owner_and_product(setup_conn, qty=5)
    bill_conn = dbmod.get_conn()
    bill = billing.start_bill(bill_conn, owner_id)
    billing.add_bill_item(bill_conn, bill["bill_id"], product_id, qty=3)
    setup_conn.close()

    def do_receive():
        conn = dbmod.get_conn()
        inventory.receive_stock(conn, product_id, qty=10, idempotency_key="race-receive-1")
        conn.close()

    def do_sale():
        conn = dbmod.get_conn()
        billing.finalize_bill(conn, bill["bill_id"], payment_mode="cash", idempotency_key="race-sale-1")
        conn.close()

    t1 = threading.Thread(target=do_receive)
    t2 = threading.Thread(target=do_sale)
    t1.start(); t2.start()
    t1.join(); t2.join()

    final_conn = dbmod.get_conn()
    final_qty = final_conn.execute("SELECT qty FROM products WHERE id=?", (product_id,)).fetchone()["qty"]
    assert final_qty == 12, f"expected 5+10-3=12 regardless of interleaving, got {final_qty}"

    # and the ledger should show both movements recorded, nothing silently dropped
    ledger_sum = final_conn.execute(
        "SELECT SUM(delta_qty) as s FROM stock_ledger WHERE product_id=?", (product_id,)
    ).fetchone()["s"]
    assert ledger_sum == 7, f"ledger deltas should sum to +10-3=7, got {ledger_sum}"


def test_retried_receive_stock_does_not_double_count():
    """This is exactly the failure mode observed live during Telegram
    testing: repeated/redelivered 'stock came in' messages silently
    inflating quantity far beyond what was actually received. Simulates a
    retried Telegram update (same update_id -> same idempotency_key) and
    proves the second call replays the cached result instead of adding again."""
    from tools import inventory
    fresh_db()
    conn = dbmod.get_conn()
    owner_id, product_id = setup_owner_and_product(conn, qty=0)

    key = "update-99:receive_stock:1"
    r1 = inventory.receive_stock(conn, product_id, qty=50, idempotency_key=key)
    r2 = inventory.receive_stock(conn, product_id, qty=50, idempotency_key=key)  # retry, same key

    assert r1 == r2, "retried receive_stock must return the identical cached result"
    final_qty = conn.execute("SELECT qty FROM products WHERE id=?", (product_id,)).fetchone()["qty"]
    assert final_qty == 50, f"expected exactly one +50 applied, got {final_qty}"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
