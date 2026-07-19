import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db import transaction


def get_preferences(conn, owner_id):
    """Called at the START of every session (not just when the model
    'remembers' to) -- this is what makes memory survive /new chat.
    See agent.py: this is invoked before the first turn, unconditionally."""
    rows = conn.execute(
        "SELECT key, value FROM preferences WHERE owner_id=?", (owner_id,)
    ).fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_preference(conn, owner_id, key, value):
    with transaction(conn):
        conn.execute(
            """INSERT INTO preferences (owner_id, key, value) VALUES (?, ?, ?)
               ON CONFLICT(owner_id, key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
            (owner_id, key, str(value)),
        )
        return {"key": key, "value": value}
