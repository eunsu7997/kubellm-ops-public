"""Local-only, bounded sanitized Alertmanager evidence sink; no external notifications."""
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

app = FastAPI(title="KubeLLM local alert evidence receiver")
DATAFILE = Path(os.environ.get("ALERT_EVENTS_FILE", "/data/events.json"))
LOCK = threading.Lock()
MAX_BYTES = 128 * 1024


def read_events():
    if not DATAFILE.exists():
        return []
    data = json.loads(DATAFILE.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("invalid event store")
    return data


@app.get("/healthz")
async def health():
    return {"status": "ok"}


@app.get("/events")
async def events():
    try:
        with LOCK:
            data = read_events()
        return {"retained_count": len(data), "events": data[-100:]}
    except (OSError, ValueError):
        raise HTTPException(503, "event store unavailable") from None


@app.post("/alerts")
async def receive(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BYTES:
            raise HTTPException(413, "payload too large")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "invalid JSON") from None
    if not isinstance(data, dict) or data.get("status") not in {"firing", "resolved"}:
        raise HTTPException(400, "invalid alert status")
    alerts = data.get("alerts")
    if not isinstance(alerts, list) or len(alerts) > 100:
        raise HTTPException(400, "invalid alerts")
    cleaned = []
    for alert in alerts:
        if not isinstance(alert, dict) or not isinstance(alert.get("labels"), dict):
            raise HTTPException(400, "invalid alert")
        labels = {key: value[:256] for key, value in alert["labels"].items()
                  if key in {"alertname", "job", "severity", "instance"} and isinstance(value, str)}
        cleaned.append({"status": alert.get("status"), "labels": labels})
    event = {"received_at": datetime.now(UTC).isoformat(), "status": data["status"], "alerts": cleaned}
    try:
        with LOCK:
            records = read_events()
            records.append(event)
            DATAFILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = DATAFILE.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(records[-1000:], handle)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(DATAFILE)
    except (OSError, ValueError):
        raise HTTPException(503, "event store unavailable") from None
    return {"accepted": len(cleaned)}
