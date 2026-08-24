import { useCallback, useEffect, useState } from "react";
import { getAlerts, type AlertListResponse } from "../api/alerts";

export function useAlerts(params: URLSearchParams) {
  const [data, setData] = useState<AlertListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const key = params.toString();
  const refresh = useCallback(async (background = false) => {
    if (!background) setLoading(true);
    try { setData(await getAlerts(new URLSearchParams(key))); setError(null); }
    catch (caught) { if ((caught as Error).name !== "AbortError") setError(caught instanceof Error ? caught.message : "No se pudieron cargar las alertas."); }
    finally { if (!background) setLoading(false); }
  }, [key]);
  useEffect(() => { const initial = window.setTimeout(() => void refresh(), 0); const poll = () => document.visibilityState === "visible" && void refresh(true); const timer = window.setInterval(poll, 30_000); return () => { window.clearTimeout(initial); window.clearInterval(timer); }; }, [refresh]);
  return { data, setData, loading, error, refresh };
}
