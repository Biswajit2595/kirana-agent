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


def make_product(conn, owner_id, qty=50):
    pid = inventory.add_product(conn, owner_id, "Parle-G", "packet", 5, 8, 10, reorder_level=5)["product_id"]
    inventory.receive_stock(conn, pid, qty, idempotency_key="seed")
    return pid


def test_cancel_bill_only_works_on_drafts():
    conn, owner_id = fresh_db()
    pid = make_product(conn, owner_id)
    b = billing.start_bill(conn, owner_id)
    billing.add_bill_item(conn, b["bill_id"], pid, 2)
    result = billing.cancel_bill(conn, b["bill_id"])
    assert result["status"] == "cancelled"


def test_cancel_bill_refuses_on_already_finalized_bill():
    conn, owner_id = fresh_db()
    pid = make_product(conn, owner_id)
    b = billing.start_bill(conn, owner_id)
    billing.add_bill_item(conn, b["bill_id"], pid, 2)
    billing.finalize_bill(conn, b["bill_id"], payment_mode="cash", idempotency_key="k1")
    try:
        billing.cancel_bill(conn, b["bill_id"])
        assert False, "should refuse to cancel an already-finalized bill"
    except ValueError:
        pass


def test_finalize_refuses_an_empty_bill():
    conn, owner_id = fresh_db()
    b = billing.start_bill(conn, owner_id)
    try:
        billing.finalize_bill(conn, b["bill_id"], payment_mode="cash", idempotency_key="k1")
        assert False, "should refuse to finalize a bill with no line items"
    except ValueError:
        pass


def test_finalize_refuses_a_bill_already_finalized_twice_with_different_keys():
    """Different from the idempotency test -- this proves that even WITHOUT
    key reuse, you cannot finalize the same bill_id a second time (someone
    fat-fingering the same bill twice shouldn't double-sell it either)."""
    conn, owner_id = fresh_db()
    pid = make_product(conn, owner_id)
    b = billing.start_bill(conn, owner_id)
    billing.add_bill_item(conn, b["bill_id"], pid, 2)
    billing.finalize_bill(conn, b["bill_id"], payment_mode="cash", idempotency_key="k1")
    try:
        billing.finalize_bill(conn, b["bill_id"], payment_mode="cash", idempotency_key="k2")  # different key!
        assert False, "should refuse -- bill is already finalized, regardless of idempotency key"
    except ValueError:
        pass


def test_edit_bill_item_recomputes_gst_correctly():
    conn, owner_id = fresh_db()
    pid = make_product(conn, owner_id)
    b = billing.start_bill(conn, owner_id)
    add_result = billing.add_bill_item(conn, b["bill_id"], pid, 2)
    preview_before = billing.preview_bill(conn, b["bill_id"])
    item_id = preview_before["items"][0]["id"]

    billing.edit_bill_item(conn, b["bill_id"], item_id, new_qty=5)
    preview_after = billing.preview_bill(conn, b["bill_id"])
    # 5 units at same unit price should be 2.5x the original line total
    assert abs(preview_after["items"][0]["line_total"] - add_result["line_total"] * 2.5) < 0.01


def test_remove_bill_item_then_finalize_only_charges_remaining_items():
    conn, owner_id = fresh_db()
    pid_a = inventory.add_product(conn, owner_id, "Maggi 70g", "packet", 12, 10, 14, reorder_level=5)["product_id"]
    pid_b = inventory.add_product(conn, owner_id, "Parle-G", "packet", 5, 8, 10, reorder_level=5)["product_id"]
    inventory.receive_stock(conn, pid_a, 20, idempotency_key="ra")
    inventory.receive_stock(conn, pid_b, 20, idempotency_key="rb")

    b = billing.start_bill(conn, owner_id)
    billing.add_bill_item(conn, b["bill_id"], pid_a, 2)
    billing.add_bill_item(conn, b["bill_id"], pid_b, 3)
    preview = billing.preview_bill(conn, b["bill_id"])
    maggi_item_id = next(i["id"] for i in preview["items"] if i["product_id"] == pid_a)

    billing.remove_bill_item(conn, b["bill_id"], maggi_item_id)
    result = billing.finalize_bill(conn, b["bill_id"], payment_mode="cash", idempotency_key="k1")

    # only Parle-G (3 units) should have been charged and decremented
    remaining_maggi = conn.execute("SELECT qty FROM products WHERE id=?", (pid_a,)).fetchone()["qty"]
    remaining_parle = conn.execute("SELECT qty FROM products WHERE id=?", (pid_b,)).fetchone()["qty"]
    assert remaining_maggi == 20, "removed item must not have its stock touched"
    assert remaining_parle == 17, "remaining item's stock must be decremented"


def test_add_bill_item_refuses_on_nonexistent_product():
    conn, owner_id = fresh_db()
    b = billing.start_bill(conn, owner_id)
    try:
        billing.add_bill_item(conn, b["bill_id"], 99999, qty=1)
        assert False, "should raise NotFound for a nonexistent product"
    except dbmod.NotFound:
        pass


def test_add_bill_item_refuses_on_nonexistent_bill():
    conn, owner_id = fresh_db()
    pid = make_product(conn, owner_id)
    try:
        billing.add_bill_item(conn, 99999, pid, qty=1)
        assert False, "should raise NotFound for a nonexistent bill"
    except dbmod.NotFound:
        pass


def test_preview_bill_aggregates_correctly_across_mixed_gst_rates():
    """Directly mirrors the brief's own example bill: loose sugar (0%),
    Aashirvaad atta (5%), Maggi (12%), Amul butter (12%) in one bill."""
    conn, owner_id = fresh_db()
    sugar = inventory.add_product(conn, owner_id, "Loose Sugar", "kg", 0, 38, 42, is_loose=True, reorder_level=5)["product_id"]
    atta = inventory.add_product(conn, owner_id, "Aashirvaad Atta 5kg", "kg", 5, 250, 280, reorder_level=5)["product_id"]
    maggi = inventory.add_product(conn, owner_id, "Maggi 70g", "packet", 12, 10, 14, reorder_level=5)["product_id"]
    butter = inventory.add_product(conn, owner_id, "Amul Butter 100g", "piece", 12, 50, 62, reorder_level=5)["product_id"]
    for pid in (sugar, atta, maggi, butter):
        inventory.receive_stock(conn, pid, 50, idempotency_key=f"r{pid}")

    b = billing.start_bill(conn, owner_id)
    billing.add_bill_item(conn, b["bill_id"], sugar, 2)
    billing.add_bill_item(conn, b["bill_id"], atta, 1)
    billing.add_bill_item(conn, b["bill_id"], maggi, 4)
    billing.add_bill_item(conn, b["bill_id"], butter, 1)

    preview = billing.preview_bill(conn, b["bill_id"])
    expected_subtotal = (42 * 2) + (280 * 1) + (14 * 4) + (62 * 1)  # 84+280+56+62 = 482
    assert abs(preview["subtotal"] - expected_subtotal) < 0.01
    assert len(preview["items"]) == 4
    # sugar's line must show zero tax while the others don't -- proves the
    # mixed-rate aggregation isn't accidentally applying one rate to everything
    sugar_line = next(i for i in preview["items"] if i["product_id"] == sugar)
    assert sugar_line["cgst_amt"] == 0 and sugar_line["sgst_amt"] == 0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
