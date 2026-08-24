from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHECKPOINT_DB = ROOT / "data" / "langgraph-checkpoints.sqlite"


def checkpoint_database_path() -> Path:
    configured = os.getenv("LANGGRAPH_CHECKPOINT_DB", "").strip()
    if not configured:
        return DEFAULT_CHECKPOINT_DB
    candidate = Path(configured).expanduser()
    return candidate if candidate.is_absolute() else ROOT / candidate


@asynccontextmanager
async def create_sqlite_checkpointer() -> AsyncIterator[AsyncSqliteSaver]:
    database_path = checkpoint_database_path()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(database_path)) as checkpointer:
        await checkpointer.setup()
        print("Checkpoint enabled: SQLite")
        print(f"Checkpoint database: {database_path}")
        yield checkpointer
