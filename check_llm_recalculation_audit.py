import sqlite3

db = r"data\workflow-events.sqlite"
call_id = "940e15ec7ba44e8b9cb345fd9b1e4265"

conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

print(f"Database: {db}")

columns = conn.execute(
    "PRAGMA table_info(llm_cost_recalculations)"
).fetchall()

print("Columns:")
for column in columns:
    print(f"  {column['name']}")

rows = conn.execute(
    """
    SELECT *
    FROM llm_cost_recalculations
    WHERE llm_call_id = ?
    ORDER BY created_at DESC
    """,
    (call_id,)
).fetchall()

print(f"Rows: {len(rows)}")

for row in rows:
    print(dict(row))

conn.close()
