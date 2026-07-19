import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import db as dbmod
from tools import khata, preferences


def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    dbmod.DB_PATH = path
    dbmod.init_db()
    conn = dbmod.get_conn()
    conn.execute("INSERT INTO owners (telegram_chat_id) VALUES ('t1')")
    owner_id = conn.execute("SELECT id FROM owners").fetchone()["id"]
    return conn, owner_id


def test_khata_pay_refuses_nonexistent_customer():
    conn, owner_id = fresh_db()
    try:
        khata.khata_pay(conn, owner_id, "Ramesh", 300, idempotency_key="p1")
        assert False, "should have raised NotFound"
    except dbmod.NotFound:
        pass  # correct — this is the guardrail from the brief


def test_khata_full_cycle():
    conn, owner_id = fresh_db()
    r1 = khata.khata_add_credit(conn, owner_id, "Ramesh", 500, idempotency_key="c1")
    assert r1["new_balance"] == 500
    r2 = khata.khata_pay(conn, owner_id, "Ramesh", 300, idempotency_key="p1")
    assert r2["new_balance"] == 200
    bal = khata.khata_balance(conn, owner_id, "Ramesh")
    assert bal["balance"] == 200


def test_preferences_survive_like_a_new_session_would():
    conn, owner_id = fresh_db()
    preferences.set_preference(conn, owner_id, "default_payment_mode", "upi")
    # simulate "/new chat" -- fresh connection, no conversation state carried,
    # only the DB persists
    conn2 = dbmod.get_conn()
    prefs = preferences.get_preferences(conn2, owner_id)
    assert prefs["default_payment_mode"] == "upi"


def test_search_product_returns_disambiguation_candidates():
    """Brief's example: owner says 'add atta' and there are two attas --
    the tool must surface BOTH as candidates so the MODEL can ask which one,
    rather than the tool (or worse, a keyword match) silently picking one."""
    from tools import inventory
    conn, owner_id = fresh_db()

    inventory.add_product(conn, owner_id, "Aashirvaad Atta 5kg", "kg", 5, 250, 280, brand="Aashirvaad", reorder_level=5)
    inventory.add_product(conn, owner_id, "Loose Atta", "kg", 0, 30, 34, is_loose=True, reorder_level=10)
    inventory.add_product(conn, owner_id, "Tata Salt 1kg", "kg", 0, 18, 22, brand="Tata", reorder_level=10)

    result = inventory.search_product(conn, owner_id, "atta")
    names = {m["name"] for m in result["matches"]}
    assert names == {"Aashirvaad Atta 5kg", "Loose Atta"}, f"expected exactly the two attas, got {names}"
    assert "Tata Salt 1kg" not in names  # unrelated product must not show up as a false match


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
