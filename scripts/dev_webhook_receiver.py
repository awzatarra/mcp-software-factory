from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
import uvicorn

from api.services.notification_adapters import sanitize_notification_data


app = FastAPI(title="Software Factory development webhook receiver")
LOG_PATH = Path(os.getenv("DEV_WEBHOOK_LOG", "data/dev-webhook-receiver.jsonl"))


@app.get("/health")
async def health(): return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    mode = request.query_params.get("mode", os.getenv("DEV_WEBHOOK_MODE", "200"))
    if mode == "timeout": await asyncio.sleep(float(os.getenv("DEV_WEBHOOK_TIMEOUT_SECONDS", "30")))
    try: payload: Any = await request.json()
    except Exception: payload = {"invalid_json": True}
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {"notification_id": request.headers.get("x-notification-id"), "idempotency_key": request.headers.get("x-idempotency-key"), "payload": sanitize_notification_data(payload)}
    with LOG_PATH.open("a", encoding="utf-8") as stream: stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    status = int(mode) if mode.isdigit() else 200
    headers = {"Retry-After": request.query_params.get("retry_after", "5")} if status in {429, 503} else {}
    return Response(content=json.dumps({"received": status < 400}), media_type="application/json", status_code=status, headers=headers)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("DEV_WEBHOOK_PORT", "8011")))
