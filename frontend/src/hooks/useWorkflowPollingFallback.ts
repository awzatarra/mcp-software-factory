import { useEffect } from "react";

import { getWorkflow, getWorkflowHistory } from "../api/workflows";
import { useWorkflowStore } from "../stores/workflowStore";

export function useWorkflowPollingFallback(
  threadId: string,
  enabled: boolean,
): void {
  useEffect(() => {
    if (!enabled) return;
    const poll = async () => {
      const state = useWorkflowStore.getState();
      const afterSequence = state.events.at(-1)?.sequence ?? 0;
      try {
        const [snapshot, history] = await Promise.all([
          getWorkflow(threadId),
          getWorkflowHistory(threadId, { afterSequence }),
        ]);
        state.setSnapshot(snapshot);
        state.appendEvents(history.events);
      } catch {
        state.setConnectionStatus("disconnected");
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 5_000);
    return () => window.clearInterval(timer);
  }, [enabled, threadId]);
}
