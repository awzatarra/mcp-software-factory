import { useCallback, useEffect, useState } from "react";
import { getNotificationSummary, type NotificationSummary } from "../api/notifications";

export function useNotificationSummary() {
  const [summary, setSummary] = useState<NotificationSummary | null>(null);
  const refresh = useCallback(async () => { try { setSummary(await getNotificationSummary()); } catch { /* Keep the last durable value while offline. */ } }, []);
  useEffect(() => { const initial = window.setTimeout(() => void refresh(), 0); const timer = window.setInterval(() => { if (document.visibilityState === "visible") void refresh(); }, 30_000); return () => { window.clearTimeout(initial); window.clearInterval(timer); }; }, [refresh]);
  return { summary, refresh };
}
