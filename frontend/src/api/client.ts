import type { ApiErrorBody, UiError } from "./types";

const DEFAULT_API_URL = "http://127.0.0.1:8000";

export function normalizeBaseUrl(value?: string): string {
  const selected = value?.trim() || DEFAULT_API_URL;
  return selected.replace(/\/+$/, "");
}

export const API_BASE_URL = normalizeBaseUrl(import.meta.env.VITE_API_BASE_URL);

export class ApiClientError extends Error {
  readonly statusCode?: number;
  readonly retryable: boolean;

  constructor(error: UiError) {
    super(error.message);
    this.name = "ApiClientError";
    this.statusCode = error.statusCode;
    this.retryable = error.retryable;
  }

  toUiError(title = "No se pudo completar la operación"): UiError {
    return {
      title,
      message: this.message,
      statusCode: this.statusCode,
      retryable: this.retryable,
    };
  }
}

function errorMessage(body: ApiErrorBody | null, status: number): string {
  if (typeof body?.detail === "string") return body.detail;
  if (Array.isArray(body?.detail)) {
    const messages = body.detail
      .map((item) => item.msg)
      .filter((value): value is string => Boolean(value));
    if (messages.length) return messages.join(". ");
  }
  if (status === 404) return "El workflow solicitado no existe.";
  if (status === 409) return "La operación ya fue resuelta o no está pendiente.";
  if (status === 422) return "La solicitud contiene datos inválidos.";
  return `La API respondió con estado ${status}.`;
}

async function parseJson<T>(response: Response): Promise<T | null> {
  if (response.status === 204) return null;
  const text = await response.text();
  if (!text.trim()) return null;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiClientError({
      title: "Respuesta inválida",
      message: "La API devolvió una respuesta que no es JSON válido.",
      statusCode: response.status,
      retryable: response.status >= 500,
    });
  }
}

export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
  timeoutMs = 15_000,
): Promise<T> {
  const controller = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => controller.abort(init.signal?.reason);
  if (init.signal?.aborted) {
    abortFromCaller();
  } else {
    init.signal?.addEventListener("abort", abortFromCaller, { once: true });
  }
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        Accept: "application/json",
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...init.headers,
      },
    });
    const body = await parseJson<T | ApiErrorBody>(response);
    if (!response.ok) {
      throw new ApiClientError({
        title: "Error de API",
        message: errorMessage(body as ApiErrorBody | null, response.status),
        statusCode: response.status,
        retryable: response.status >= 500,
      });
    }
    return body as T;
  } catch (error) {
    if (error instanceof ApiClientError) throw error;
    if (
      error instanceof DOMException &&
      error.name === "AbortError" &&
      init.signal?.aborted &&
      !timedOut
    ) {
      throw error;
    }
    const offline = error instanceof TypeError;
    const timeoutReached =
      error instanceof DOMException && error.name === "AbortError";
    throw new ApiClientError({
      title: offline ? "API no disponible" : "Tiempo de espera agotado",
      message: offline
        ? "No se pudo conectar con la API de Software Factory."
        : timeoutReached
          ? "La API tardó demasiado en responder."
          : "Ocurrió un error de red inesperado.",
      retryable: true,
    });
  } finally {
    window.clearTimeout(timeout);
    init.signal?.removeEventListener("abort", abortFromCaller);
  }
}
