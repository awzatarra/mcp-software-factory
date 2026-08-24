import sqlite3

db = r"data\workflow-events.sqlite"

alert_ids = (
    "d7baadd0-21c4-4a22-9d38-4f6ca7ec5ae0",
    "6bbecf2d-6ba1-479d-9092-4c345d5798ff",
)

conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

tables = conn.execute("""
    SELECT name
    FROM sqlite_master
    WHERE type='table'
      AND name LIKE '%alert%'
    ORDER BY name
""").fetchall()

print("Alert tables:")
for table in tables:
    print(" ", table["name"])

for table in tables:
    table_name = table["name"]

    columns = [
        row["name"]
        for row in conn.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    ]

    if "alert_id" not in columns:
        continue

    placeholders = ",".join("?" for _ in alert_ids)

    rows = conn.execute(
        f"""
        SELECT *
        FROM {table_name}
        WHERE alert_id IN ({placeholders})
        """,
        alert_ids,
    ).fetchall()

    if rows:
        print()
        print(f"Table: {table_name}")
        for row in rows:
            data = dict(row)

            interesting = {
                key: data.get(key)
                for key in (
                    "alert_id",
                    "rule_id",
                    "status",
                    "occurrence_count",
                    "resolved_at",
                    "resolved_by",
                    "resolution_note",
                    "first_detected_at",
                    "last_detected_at",
                )
                if key in data
            }

            print(interesting)

conn.close()
