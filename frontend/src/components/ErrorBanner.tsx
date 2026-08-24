import { CircleAlert, RotateCcw, X } from "lucide-react";

import type { UiError } from "../api/types";

interface ErrorBannerProps {
  error: UiError;
  onRetry?: () => void;
  onDismiss: () => void;
}

export function ErrorBanner({
  error,
  onRetry,
  onDismiss,
}: ErrorBannerProps) {
  return (
    <div className="error-banner" role="alert">
      <CircleAlert aria-hidden="true" />
      <div>
        <strong>{error.title}</strong>
        <p>{error.message}</p>
      </div>
      {error.retryable && onRetry ? (
        <button className="text-button" onClick={onRetry} type="button">
          <RotateCcw aria-hidden="true" />
          Reintentar
        </button>
      ) : null}
      <button
        className="icon-button"
        onClick={onDismiss}
        type="button"
        aria-label="Cerrar error"
        title="Cerrar"
      >
        <X aria-hidden="true" />
      </button>
    </div>
  );
}
