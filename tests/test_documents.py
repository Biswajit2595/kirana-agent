import sys, os, tempfile, datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import db as dbmod
from tools import inventory, billing, documents


def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    dbmod.DB_PATH = path
    dbmod.init_db()
    conn = dbmod.get_conn()
    conn.execute("INSERT INTO owners (telegram_chat_id, shop_name, gstin) VALUES ('t1','Test Store','27TESTGSTIN1Z5')")
    owner_id = conn.execute("SELECT id FROM owners").fetchone()["id"]
    return conn, owner_id


def make_and_finalize_bill(conn, owner_id):
    pid = inventory.add_product(conn, owner_id, "Aashirvaad Atta 5kg", "kg", 5, 250, 280, reorder_level=5)["product_id"]
    inventory.receive_stock(conn, pid, 20, idempotency_key="r1")
    b = billing.start_bill(conn, owner_id, customer_name="Walk-in")
    billing.add_bill_item(conn, b["bill_id"], pid, 2)
    billing.finalize_bill(conn, b["bill_id"], payment_mode="upi", payment_ref="UPI999", idempotency_key="f1")
    return b["bill_id"]


def test_invoice_pdf_is_created_and_non_trivial():
    conn, owner_id = fresh_db()
    bill_id = make_and_finalize_bill(conn, owner_id)
    result = documents.generate_invoice_pdf(conn, bill_id, shop_name="Test Store", gstin="27TESTGSTIN1Z5")
    assert os.path.exists(result["file_path"])
    assert os.path.getsize(result["file_path"]) > 1000, "PDF suspiciously small -- likely empty/broken"


def test_invoice_pdf_refuses_for_a_draft_unfinalized_bill():
    conn, owner_id = fresh_db()
    pid = inventory.add_product(conn, owner_id, "Loose Sugar", "kg", 0, 38, 42, reorder_level=5)["product_id"]
    inventory.receive_stock(conn, pid, 10, idempotency_key="r1")
    b = billing.start_bill(conn, owner_id)
    billing.add_bill_item(conn, b["bill_id"], pid, 1)
    # never finalized
    try:
        documents.generate_invoice_pdf(conn, b["bill_id"])
        assert False, "should refuse to invoice a draft bill"
    except dbmod.NotFound:
        pass


def test_invoice_pdf_refuses_for_nonexistent_bill():
    conn, owner_id = fresh_db()
    try:
        documents.generate_invoice_pdf(conn, 99999)
        assert False, "should raise NotFound for a bill id that doesn't exist"
    except dbmod.NotFound:
        pass


def test_analysis_deck_is_created_and_non_trivial():
    conn, owner_id = fresh_db()
    make_and_finalize_bill(conn, owner_id)
    today = datetime.date.today().isoformat()
    result = documents.generate_analysis_deck(conn, owner_id, today, today, shop_name="Test Store")
    assert os.path.exists(result["file_path"])
    assert os.path.getsize(result["file_path"]) > 5000, "PPTX suspiciously small -- likely missing charts"


def test_analysis_deck_handles_a_period_with_zero_sales_gracefully():
    """Brief-relevant: the deck must not crash just because a date range had
    no activity -- it should still produce a valid, presentable file."""
    conn, owner_id = fresh_db()
    inventory.add_product(conn, owner_id, "Tata Salt 1kg", "kg", 0, 18, 22, reorder_level=10)
    result = documents.generate_analysis_deck(conn, owner_id, "2020-01-01", "2020-01-02", shop_name="Test Store")
    assert os.path.exists(result["file_path"])


def test_analysis_deck_reflects_low_stock_items():
    conn, owner_id = fresh_db()
    pid = inventory.add_product(conn, owner_id, "Amul Butter 100g", "piece", 12, 50, 62, reorder_level=20)["product_id"]
    inventory.receive_stock(conn, pid, 5, idempotency_key="r1")  # below reorder_level of 20
    today = datetime.date.today().isoformat()
    result = documents.generate_analysis_deck(conn, owner_id, today, today, shop_name="Test Store")
    assert os.path.exists(result["file_path"])
    # not asserting deck CONTENT (would need pptx parsing) -- covered at the
    # data layer by test_reporting/inventory tests; this just proves the
    # tool doesn't crash when low-stock items exist


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
