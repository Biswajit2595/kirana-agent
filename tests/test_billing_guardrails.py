import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import db as dbmod
from tools import inventory, billing


def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    dbmod.DB_PATH = path
    dbmod.init_db()
    conn = dbmod.get_conn()
    conn.execute("INSERT INTO owners (telegram_chat_id) VALUES ('t1')")
    owner_id = conn.execute("SELECT id FROM owners").fetchone()["id"]
    return conn, owner_id


def test_below_cost_sale_is_blocked_without_confirmation():
    conn, owner_id = fresh_db()
    # sell_price (8) deliberately below cost_price (10)
    pid = inventory.add_product(conn, owner_id, "Clearance Item", "piece", 12, 10, 8, reorder_level=0)["product_id"]
    inventory.receive_stock(conn, pid, 20)
    bill = billing.start_bill(conn, owner_id)

    try:
        billing.add_bill_item(conn, bill["bill_id"], pid, qty=1)
        assert False, "should have raised BelowCostSale"
    except dbmod.BelowCostSale as e:
        assert e.sell_price == 8 and e.cost_price == 10

    # with explicit confirmation, it goes through
    result = billing.add_bill_item(conn, bill["bill_id"], pid, qty=1, confirm_below_cost=True)
    assert "line_total" in result


def test_stock_warning_flagged_but_not_blocking_at_add_time():
    conn, owner_id = fresh_db()
    pid = inventory.add_product(conn, owner_id, "Maggi 70g", "packet", 12, 10, 14, reorder_level=5)["product_id"]
    inventory.receive_stock(conn, pid, 3)  # only 3 in stock
    bill = billing.start_bill(conn, owner_id)

    result = billing.add_bill_item(conn, bill["bill_id"], pid, qty=10)  # asking for more than exists
    assert result["stock_warning"] is True
    assert result["available_stock"] == 3

    # the DRAFT add succeeds (informational only) -- but finalize must still refuse
    try:
        billing.finalize_bill(conn, bill["bill_id"], payment_mode="cash", idempotency_key="k-warn-1")
        assert False, "finalize should still enforce the real oversell guard"
    except dbmod.InsufficientStock:
        pass


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
