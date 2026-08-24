from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
import io
import json

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from api.services.workflow_execution_service import WorkflowExecutionService
from api.services.workflow_metadata_store import WorkflowMetadataStore
from api.services.alert_service import AlertEvaluationService
from api.services.alert_store import AlertStore
from graph.builder import build_software_factory_graph
from graph.checkpointing import checkpoint_database_path
from graph.nodes import GraphDependencies
from graph.persistence_service import WorkflowPersistenceService
from streaming.sqlite_store import workflow_event_store_path
from streaming.sqlite_store import SQLiteWorkflowEventStore


async def main() -> None:
    event_database = workflow_event_store_path()
    store = AlertStore(event_database)
    event_store = SQLiteWorkflowEventStore(event_database)
    await store.initialize()
    await event_store.initialize()
    try:
        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_database_path())) as checkpointer:
            await checkpointer.setup()
            dependencies = GraphDependencies(
                tool_executor=None,  # type: ignore[arg-type]
                openai_client=None,  # type: ignore[arg-type]
                model="backfill-read-only",
            )
            graph = build_software_factory_graph(dependencies, checkpointer=checkpointer)
            execution = WorkflowExecutionService(
                persistence=WorkflowPersistenceService(graph),
                event_store=event_store,
                metadata_store=WorkflowMetadataStore(event_database),
            )
            with redirect_stdout(io.StringIO()):
                result = await AlertEvaluationService(store, execution).backfill()
    finally:
        await event_store.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
