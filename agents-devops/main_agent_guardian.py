#!/usr/bin/env python3
from typing import Any
from contextlib import asynccontextmanager
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from src.agent_guardian.runtime import SREGuardianRuntime
from src.agent_guardian.config import LOGGER

runtime = SREGuardianRuntime()

@asynccontextmanager
async def lifespan(_: FastAPI):
    await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()

app = FastAPI(
    title="ATLAS SRE Guardian",
    description="Webhook-driven SRE agent using LangGraph + MCP",
    version="1.0.0",
    lifespan=lifespan,
)

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

async def _process_alerts_in_background(alerts: list[dict[str, Any]]) -> None:
    for idx, alert in enumerate(alerts):
        try:
            await runtime.process_alert(alert, idx)
        except Exception as exc:
            LOGGER.error(
                "Unhandled background agent execution error",
                extra={
                    "index": idx,
                    "alert_name": (alert.get("labels") or {}).get("alertname", "unknown"),
                    "deployment": (alert.get("labels") or {}).get("service", ""),
                    "error": str(exc),
                },
            )

@app.post("/webhook")
async def webhook(payload: dict[str, Any], background_tasks: BackgroundTasks) -> JSONResponse:
    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        raise HTTPException(status_code=400, detail="Invalid Alertmanager payload: missing alerts list")

    firing_alerts = [a for a in alerts if isinstance(a, dict) and a.get("status", "firing") == "firing"]

    LOGGER.info(
        "Webhook received",
        extra={
            "receiver": payload.get("receiver", ""),
            "status": payload.get("status", ""),
            "alerts_total": len(alerts),
            "alerts_firing": len(firing_alerts),
        },
    )

    background_tasks.add_task(_process_alerts_in_background, firing_alerts)
    return JSONResponse(
        status_code=200,
        content={"status": "accepted", "message": "Alerts queued for background processing"},
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_agent_guardian:app", host="0.0.0.0", port=8000, log_level="info")
