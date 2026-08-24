import sqlite3

db = r"data\workflow-events.sqlite"
reservation_id = "c488a109c3f94f6399bb86244e924706"

conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

row = conn.execute(
    """
    SELECT *
    FROM llm_budget_reservations
    WHERE reservation_id = ?
    """,
    (reservation_id,)
).fetchone()

print(dict(row) if row else "NOT_FOUND")

conn.close()
