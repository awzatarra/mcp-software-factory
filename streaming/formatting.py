from streaming.catalog import WorkflowEventType
from streaming.models import WorkflowEvent

_LABELS = {
    WorkflowEventType.WORKFLOW_STARTED: "Workflow started",
    WorkflowEventType.WORKFLOW_RESUMED: "Workflow resumed",
    WorkflowEventType.WORKFLOW_FORKED: "Workflow forked",
    WorkflowEventType.WORKFLOW_COMPLETED: "Workflow completed",
    WorkflowEventType.WORKFLOW_FAILED: "Workflow failed",
    WorkflowEventType.APPROVAL_REQUIRED: "Waiting approval",
    WorkflowEventType.APPROVAL_GRANTED: "Approval granted",
    WorkflowEventType.APPROVAL_REJECTED: "Approval rejected",
    WorkflowEventType.HANDOFF_COMPLETED: "Supervisor handoff",
}


def format_terminal_event(event: WorkflowEvent) -> str:
    label = _LABELS.get(
        event.type,
        event.type.value.replace("_", " ").capitalize(),
    )
    detail = event.message
    if detail is None:
        detail = (
            event.data.get("project_name")
            or event.data.get("executed_target")
            or event.data.get("operation")
            or event.data.get("tool")
        )
    suffix = f": {detail}" if detail not in (None, "") else ""
    return f"[{event.sequence:03d}] {label}{suffix}"
