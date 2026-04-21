#!/usr/bin/env python3
from typing import Any
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from src.agent_guardian.runtime import SREGuardianRuntime
from src.agent_guardian.config import LOGGER
from src.agent_guardian.utils import _safe_json

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

@app.post("/webhook")
async def webhook(payload: dict[str, Any]) -> JSONResponse:
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

    if not firing_alerts:
        return JSONResponse(
            status_code=200,
            content={"status": "ignored", "message": "No firing alerts in payload", "results": []},
        )

    results: list[dict[str, Any]] = []
    for idx, alert in enumerate(firing_alerts):
        try:
            final_state = await runtime.graph.ainvoke({"alert": alert})
            results.append(
                {
                    "index": idx,
                    "alert_name": (final_state.get("parsed_alert") or {}).get("alert_name", "unknown"),
                    "deployment": (final_state.get("parsed_alert") or {}).get("deployment", ""),
                    "investigation_report": final_state.get("investigation_report", ""),
                    "final_report": final_state.get("final_report", ""),
                    "error": final_state.get("error", ""),
                }
            )
        except Exception as exc:
            LOGGER.error("Unhandled agent execution error", extra={"index": idx, "error": str(exc)})
            results.append(
                {
                    "index": idx,
                    "alert_name": (alert.get("labels") or {}).get("alertname", "unknown"),
                    "deployment": (alert.get("labels") or {}).get("service", ""),
                    "investigation_report": "",
                    "final_report": _safe_json(
                        {
                            "action": "HOLD",
                            "rationale": "Internal agent exception during execution",
                            "outcome": "No action",
                        }
                    ),
                    "error": str(exc),
                }
            )

    return JSONResponse(status_code=200, content={"status": "processed", "results": results})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_agent_guardian:app", host="0.0.0.0", port=8000, log_level="info")
