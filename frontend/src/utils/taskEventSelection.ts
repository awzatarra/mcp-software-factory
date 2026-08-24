import type {
  WorkflowEvent,
  WorkflowExecutionTask,
} from "../api/types";

const QA_EVENT_PRECEDENCE = [
  "test_run_completed",
  "testing_completed",
  "tool_completed",
  "test_run_started",
  "approval_required",
  "testing_started",
];

const QA_AGENTS = new Set([
  "qa",
  "qa reviewer",
  "quality assurance",
  "test engineer",
  "tester",
  "testing agent",
]);

function normalizedAgent(agent: string): string {
  return agent.trim().toLocaleLowerCase().replace(/\s+/g, " ");
}

export function selectPreferredTaskEvent(
  task: WorkflowExecutionTask,
  events: WorkflowEvent[],
): string | null {
  if (task.primary_event_id) return task.primary_event_id;
  const related = new Set(task.related_event_ids);
  const candidates = events.filter((event) => related.has(event.event_id));
  if (QA_AGENTS.has(normalizedAgent(task.agent))) {
    for (const type of QA_EVENT_PRECEDENCE) {
      const selected = candidates.find((event) => event.type === type);
      if (selected) return selected.event_id;
    }
  }
  return task.related_event_ids[0] ?? null;
}
