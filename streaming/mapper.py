from collections.abc import Mapping, Sequence
from typing import Any

from streaming.catalog import WorkflowEventType
from streaming.factory import WorkflowEventFactory
from streaming.models import EventStatus, WorkflowEvent


class LangGraphEventMapper:
    def __init__(
        self,
        factory: WorkflowEventFactory,
        *,
        base_data: Mapping[str, Any] | None = None,
    ) -> None:
        self.factory = factory
        self.base_data = dict(base_data or {})
        self._emitted: set[tuple[object, ...]] = set()

    def _create(
        self,
        *,
        thread_id: str,
        event_type: WorkflowEventType,
        stage: str | None,
        status: EventStatus,
        namespace: Sequence[str],
        node_name: str,
        data: Mapping[str, Any] | None = None,
    ) -> list[WorkflowEvent]:
        payload = {
            **self.base_data,
            "namespace": list(namespace),
            "parent_stage": namespace[0] if namespace else None,
            "subgraph": namespace[0] if namespace else None,
            "node": node_name,
            **dict(data or {}),
        }
        key = (thread_id, event_type, stage, node_name, payload.get("branch_id"))
        if key in self._emitted:
            return []
        self._emitted.add(key)
        return [
            self.factory.create(
                thread_id=thread_id,
                event_type=event_type,
                source="langgraph.update",
                stage=stage,
                status=status,
                data=payload,
            )
        ]

    def map_update(
        self,
        *,
        thread_id: str,
        namespace: Sequence[str],
        node_name: str,
        update: Mapping[str, Any],
    ) -> list[WorkflowEvent]:
        events: list[WorkflowEvent] = []
        planning = update.get("planning_result")
        implementation = update.get("implementation_result")
        testing = update.get("testing_result")

        if isinstance(planning, Mapping):
            valid = planning.get("valid")
            if valid is None:
                valid = planning.get("is_valid")
            event_type = WorkflowEventType.PLANNING_COMPLETED if valid is not False else WorkflowEventType.PLANNING_FAILED
            status = EventStatus.COMPLETED if valid is not False else EventStatus.FAILED
            events += self._create(
                thread_id=thread_id,
                event_type=event_type,
                stage="planning",
                status=status,
                namespace=namespace,
                node_name=node_name,
                data={"valid": valid},
            )

        if isinstance(implementation, Mapping) and implementation.get("project_created") is True:
            events += self._create(
                thread_id=thread_id,
                event_type=WorkflowEventType.IMPLEMENTATION_COMPLETED,
                stage="implementation",
                status=EventStatus.COMPLETED,
                namespace=namespace,
                node_name=node_name,
                data={"project_created": True},
            )

        if isinstance(testing, Mapping) and testing.get("tests_passed") is not None:
            passed = testing.get("tests_passed") is True
            events += self._create(
                thread_id=thread_id,
                event_type=WorkflowEventType.TESTING_COMPLETED if passed else WorkflowEventType.TESTING_FAILED,
                stage="testing_repair",
                status=EventStatus.COMPLETED if passed else EventStatus.FAILED,
                namespace=namespace,
                node_name=node_name,
                data={"tests_passed": passed},
            )

        if node_name == "detect_intent":
            events += self._create(
                thread_id=thread_id,
                event_type=WorkflowEventType.STAGE_COMPLETED,
                stage="detect_intent",
                status=EventStatus.COMPLETED,
                namespace=namespace,
                node_name=node_name,
            )

        terminal_status = update.get("terminal_status")
        if node_name == "finalize" and terminal_status is not None:
            completed = terminal_status == "completed"
            events += self._create(
                thread_id=thread_id,
                event_type=WorkflowEventType.WORKFLOW_COMPLETED if completed else WorkflowEventType.WORKFLOW_FAILED,
                stage="finalize",
                status=EventStatus.COMPLETED if completed else EventStatus.FAILED,
                namespace=namespace,
                node_name=node_name,
                data={"terminal_status": terminal_status},
            )
        return events
