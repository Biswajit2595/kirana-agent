import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import db as dbmod
from tools import inventory


def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    dbmod.DB_PATH = path
    dbmod.init_db()
    conn = dbmod.get_conn()
    conn.execute("INSERT INTO owners (telegram_chat_id) VALUES ('t1')")
    owner_id = conn.execute("SELECT id FROM owners").fetchone()["id"]
    return conn, owner_id


def test_low_stock_report_only_includes_items_at_or_below_reorder_level():
    conn, owner_id = fresh_db()
    low = inventory.add_product(conn, owner_id, "Amul Butter 100g", "piece", 12, 50, 62, reorder_level=20)["product_id"]
    healthy = inventory.add_product(conn, owner_id, "Tata Salt 1kg", "kg", 0, 18, 22, reorder_level=10)["product_id"]

    inventory.receive_stock(conn, low, 5, idempotency_key="r1")       # 5 <= 20 -> low
    inventory.receive_stock(conn, healthy, 100, idempotency_key="r2")  # 100 > 10 -> healthy

    result = inventory.low_stock_report(conn, owner_id)
    names = {p["name"] for p in result["low_stock"]}
    assert names == {"Amul Butter 100g"}


def test_low_stock_report_exactly_at_reorder_level_counts_as_low():
    """Boundary case: qty == reorder_level should count as needing reorder,
    not just strictly below it."""
    conn, owner_id = fresh_db()
    pid = inventory.add_product(conn, owner_id, "Maggi 70g", "packet", 12, 10, 14, reorder_level=20)["product_id"]
    inventory.receive_stock(conn, pid, 20, idempotency_key="r1")  # exactly at reorder level
    result = inventory.low_stock_report(conn, owner_id)
    assert len(result["low_stock"]) == 1


def test_adjust_stock_refuses_to_go_negative():
    conn, owner_id = fresh_db()
    pid = inventory.add_product(conn, owner_id, "Parle-G", "packet", 5, 8, 10, reorder_level=5)["product_id"]
    inventory.receive_stock(conn, pid, 3, idempotency_key="r1")
    try:
        inventory.adjust_stock(conn, pid, delta=-10, reason="damaged goods", idempotency_key="a1")
        assert False, "should refuse an adjustment that would take stock negative"
    except ValueError:
        pass
    remaining = conn.execute("SELECT qty FROM products WHERE id=?", (pid,)).fetchone()["qty"]
    assert remaining == 3, "refused adjustment must not partially apply"


def test_adjust_stock_requires_a_reason():
    conn, owner_id = fresh_db()
    pid = inventory.add_product(conn, owner_id, "Parle-G", "packet", 5, 8, 10, reorder_level=5)["product_id"]
    inventory.receive_stock(conn, pid, 10, idempotency_key="r1")
    try:
        inventory.adjust_stock(conn, pid, delta=-2, reason="", idempotency_key="a1")
        assert False, "should require a non-empty reason"
    except ValueError:
        pass


def test_adjust_stock_positive_correction_is_logged_to_ledger():
    conn, owner_id = fresh_db()
    pid = inventory.add_product(conn, owner_id, "Parle-G", "packet", 5, 8, 10, reorder_level=5)["product_id"]
    inventory.receive_stock(conn, pid, 10, idempotency_key="r1")
    inventory.adjust_stock(conn, pid, delta=5, reason="recount found extra stock", idempotency_key="a1")

    final_qty = conn.execute("SELECT qty FROM products WHERE id=?", (pid,)).fetchone()["qty"]
    assert final_qty == 15
    ledger_reasons = [r["reason"] for r in conn.execute(
        "SELECT reason FROM stock_ledger WHERE product_id=?", (pid,)
    ).fetchall()]
    assert "adjustment" in ledger_reasons, "correction must be logged, not silently applied"


def test_search_product_is_case_insensitive():
    conn, owner_id = fresh_db()
    inventory.add_product(conn, owner_id, "Aashirvaad Atta 5kg", "kg", 5, 250, 280, reorder_level=5)
    result = inventory.search_product(conn, owner_id, "AASHIRVAAD")
    assert len(result["matches"]) == 1


def test_search_product_matches_by_brand_too():
    conn, owner_id = fresh_db()
    inventory.add_product(conn, owner_id, "Butter 100g", "piece", 12, 50, 62, brand="Amul", reorder_level=5)
    result = inventory.search_product(conn, owner_id, "Amul")
    assert len(result["matches"]) == 1


def test_receive_stock_updates_cost_price_only_when_provided():
    conn, owner_id = fresh_db()
    pid = inventory.add_product(conn, owner_id, "Parle-G", "packet", 5, 8, 10, reorder_level=5)["product_id"]
    inventory.receive_stock(conn, pid, 10, idempotency_key="r1")  # no new_cost_price
    unchanged = conn.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()["cost_price"]
    assert unchanged == 8

    inventory.receive_stock(conn, pid, 10, new_cost_price=9.5, idempotency_key="r2")
    updated = conn.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()["cost_price"]
    assert updated == 9.5


def test_check_stock_returns_correct_shape():
    conn, owner_id = fresh_db()
    pid = inventory.add_product(conn, owner_id, "Maggi 70g", "packet", 12, 10, 14, reorder_level=20)["product_id"]
    inventory.receive_stock(conn, pid, 35, idempotency_key="r1")
    result = inventory.check_stock(conn, pid)
    assert result["qty"] == 35
    assert result["unit"] == "packet"
    assert result["reorder_level"] == 20
    assert result["name"] == "Maggi 70g"


def test_check_stock_refuses_for_nonexistent_product():
    conn, owner_id = fresh_db()
    try:
        inventory.check_stock(conn, 99999)
        assert False, "should raise NotFound for a nonexistent product"
    except dbmod.NotFound:
        pass


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
