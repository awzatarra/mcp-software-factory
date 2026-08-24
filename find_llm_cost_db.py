import sqlite3
from pathlib import Path

files = (
    list(Path(".").rglob("*.db")) +
    list(Path(".").rglob("*.sqlite")) +
    list(Path(".").rglob("*.sqlite3"))
)

print(f"SQLite encontrados: {len(files)}")

for file in files:
    try:
        conn = sqlite3.connect(file)
        found = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table'
              AND name='llm_cost_recalculations'
            """
        ).fetchone()

        print(f"{file} -> {bool(found)}")
        conn.close()
    except Exception as exc:
        print(f"{file} -> ERROR: {exc}")
