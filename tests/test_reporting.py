import sys, os, tempfile, datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import db as dbmod
from tools import inventory, billing, reporting


def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    dbmod.DB_PATH = path
    dbmod.init_db()
    conn = dbmod.get_conn()
    conn.execute("INSERT INTO owners (telegram_chat_id) VALUES ('t1')")
    owner_id = conn.execute("SELECT id FROM owners").fetchone()["id"]
    return conn, owner_id


def make_product(conn, owner_id, name="Maggi 70g", cost=10, sell=14, gst=12):
    return inventory.add_product(conn, owner_id, name, "packet", gst, cost, sell, reorder_level=5)["product_id"]


def sell(conn, owner_id, product_id, qty, payment_mode, key):
    b = billing.start_bill(conn, owner_id)
    billing.add_bill_item(conn, b["bill_id"], product_id, qty)
    return billing.finalize_bill(conn, b["bill_id"], payment_mode=payment_mode, idempotency_key=key)


def test_daily_close_only_counts_finalized_bills():
    conn, owner_id = fresh_db()
    pid = make_product(conn, owner_id)
    inventory.receive_stock(conn, pid, 100, idempotency_key="r1")

    sell(conn, owner_id, pid, 2, "cash", "k1")  # counted
    draft = billing.start_bill(conn, owner_id)   # NOT finalized -- must be excluded
    billing.add_bill_item(conn, draft["bill_id"], pid, 5)
    cancelled = billing.start_bill(conn, owner_id)
    billing.add_bill_item(conn, cancelled["bill_id"], pid, 3)
    billing.cancel_bill(conn, cancelled["bill_id"])  # NOT finalized -- must be excluded

    today = datetime.date.today().isoformat()
    result = reporting.daily_close(conn, owner_id, today)
    assert result["bill_count"] == 1, f"expected only the 1 finalized bill counted, got {result['bill_count']}"


def test_sales_range_aggregates_multiple_finalized_bills():
    conn, owner_id = fresh_db()
    pid = make_product(conn, owner_id)
    inventory.receive_stock(conn, pid, 100, idempotency_key="r1")

    r1 = sell(conn, owner_id, pid, 2, "cash", "k1")
    r2 = sell(conn, owner_id, pid, 3, "upi", "k2")

    today = datetime.date.today().isoformat()
    result = reporting.sales_range(conn, owner_id, today, today)
    assert result["bill_count"] == 2
    assert abs(result["total_sales"] - (r1["grand_total"] + r2["grand_total"])) < 0.01


def test_sales_range_payment_mode_split_is_correct():
    conn, owner_id = fresh_db()
    pid = make_product(conn, owner_id)
    inventory.receive_stock(conn, pid, 100, idempotency_key="r1")

    r_cash = sell(conn, owner_id, pid, 1, "cash", "k1")
    r_upi_1 = sell(conn, owner_id, pid, 1, "upi", "k2")
    r_upi_2 = sell(conn, owner_id, pid, 1, "upi", "k3")

    today = datetime.date.today().isoformat()
    result = reporting.sales_range(conn, owner_id, today, today)
    assert abs(result["by_payment_mode"]["cash"] - r_cash["grand_total"]) < 0.01
    expected_upi = r_upi_1["grand_total"] + r_upi_2["grand_total"]
    assert abs(result["by_payment_mode"]["upi"] - expected_upi) < 0.01


def test_top_items_ordered_by_quantity_sold_descending():
    conn, owner_id = fresh_db()
    pid_a = make_product(conn, owner_id, name="Low Seller")
    pid_b = make_product(conn, owner_id, name="High Seller")
    inventory.receive_stock(conn, pid_a, 100, idempotency_key="ra")
    inventory.receive_stock(conn, pid_b, 100, idempotency_key="rb")

    sell(conn, owner_id, pid_a, 2, "cash", "k1")
    sell(conn, owner_id, pid_b, 20, "cash", "k2")

    today = datetime.date.today().isoformat()
    result = reporting.sales_range(conn, owner_id, today, today)
    names_in_order = [i["product"] for i in result["top_items"]]
    assert names_in_order[0] == "High Seller", f"expected High Seller first, got {names_in_order}"


def test_daily_close_defaults_to_today_when_no_date_given():
    conn, owner_id = fresh_db()
    pid = make_product(conn, owner_id)
    inventory.receive_stock(conn, pid, 10, idempotency_key="r1")
    sell(conn, owner_id, pid, 1, "cash", "k1")

    result = reporting.daily_close(conn, owner_id)  # no date argument
    assert result["bill_count"] == 1


def test_sales_range_with_no_sales_returns_zeroed_totals_not_an_error():
    conn, owner_id = fresh_db()
    result = reporting.sales_range(conn, owner_id, "2020-01-01", "2020-01-02")
    assert result["total_sales"] == 0
    assert result["bill_count"] == 0
    assert result["top_items"] == []


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
