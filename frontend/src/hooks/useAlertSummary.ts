import { useCallback, useEffect, useState } from "react";
import { getAlertSummary, type AlertSummary } from "../api/alerts";

export function useAlertSummary() {
  const [summary, setSummary] = useState<AlertSummary | null>(null);
  const refresh = useCallback(async () => { try { setSummary(await getAlertSummary()); } catch { /* Keep the last durable value while offline. */ } }, []);
  useEffect(() => {
    const initial = window.setTimeout(() => void refresh(), 0);
    const poll = () => { if (document.visibilityState === "visible") void refresh(); };
    const timer = window.setInterval(poll, 30_000);
    document.addEventListener("visibilitychange", poll);
    return () => { window.clearTimeout(initial); window.clearInterval(timer); document.removeEventListener("visibilitychange", poll); };
  }, [refresh]);
  return { summary, refresh };
}
