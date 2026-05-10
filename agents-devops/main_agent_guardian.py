#!/usr/bin/env python3
from typing import Any
from contextlib import asynccontextmanager
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from src.agent_guardian.runtime import SREGuardianRuntime
from src.agent_guardian.config import LOGGER
from src.database import (
    create_approval_request,
    fetch_pending_approvals,
    update_approval_status,
    fetch_incident_report,
    upsert_incident_report,
    get_async_session,
)
from datetime import datetime, timezone
import uuid
import asyncio

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


@app.get("/approvals")
async def list_approvals() -> dict[str, Any]:
    async with get_async_session() as session:
        pending = await fetch_pending_approvals(session)
    return {"count": len(pending), "approvals": pending}


@app.post("/approvals/{approval_id}/approve")
async def approve(approval_id: str, payload: dict[str, Any] | None = None) -> JSONResponse:
    approver = (payload or {}).get("approver") or "human"
    reason = (payload or {}).get("reason") or "approved"
    decision_at = datetime.now(timezone.utc)

    async with get_async_session() as session:
        pending = await fetch_pending_approvals(session)
        target = next((p for p in pending if p.get("approval_id") == approval_id), None)
        incident_id = target.get("incident_id") if target else None

        await update_approval_status(
            session,
            approval_id=approval_id,
            status="APPROVED",
            approver=approver,
            decision_at=decision_at,
            decision_reason=reason,
        )

        if incident_id:
            existing = await fetch_incident_report(session, incident_id) or {}
            if existing:
                # mark the incident execution_details approval state
                existing.setdefault("execution_details", {})
                existing["execution_details"]["human_approval"] = "approved"
                existing["execution_details"]["outcome"] = existing.get("execution_details", {}).get("outcome", "") or "PENDING_APPROVAL"
                await upsert_incident_report(
                    session,
                    incident_id=incident_id,
                    timestamp_utc=decision_at,
                    deployment=existing.get("alert_context", {}).get("deployment", "unknown"),
                    report_data=existing,
                )

    return JSONResponse(status_code=200, content={"status": "approved", "approval_id": approval_id})


@app.post("/approvals/{approval_id}/deny")
async def deny(approval_id: str, payload: dict[str, Any] | None = None) -> JSONResponse:
    approver = (payload or {}).get("approver") or "human"
    reason = (payload or {}).get("reason") or "denied"
    decision_at = datetime.now(timezone.utc)

    async with get_async_session() as session:
        pending = await fetch_pending_approvals(session)
        target = next((p for p in pending if p.get("approval_id") == approval_id), None)
        incident_id = target.get("incident_id") if target else None

        await update_approval_status(
            session,
            approval_id=approval_id,
            status="DENIED",
            approver=approver,
            decision_at=decision_at,
            decision_reason=reason,
        )

        if incident_id:
            existing = await fetch_incident_report(session, incident_id) or {}
            if existing:
                existing.setdefault("execution_details", {})
                existing["execution_details"]["human_approval"] = "denied"
                existing["execution_details"]["outcome"] = existing.get("execution_details", {}).get("outcome", "") or "DENIED_BY_HUMAN"
                await upsert_incident_report(
                    session,
                    incident_id=incident_id,
                    timestamp_utc=decision_at,
                    deployment=existing.get("alert_context", {}).get("deployment", "unknown"),
                    report_data=existing,
                )

    return JSONResponse(status_code=200, content={"status": "denied", "approval_id": approval_id})



@app.post("/approvals/{approval_id}/execute")
async def execute_approval(approval_id: str, payload: dict[str, Any] | None = None) -> JSONResponse:
    """Execute automation-safe actions for a pending approval request.

    Currently supported automatic action: scaling via ``set_replicas``.
    For manual actions (e.g. rollback recommendation), this endpoint returns
    ``manual_action_required`` and no cluster change is performed.
    """
    # find approval and deployment/action context (fetch by id so APPROVED approvals are reachable)
    async with get_async_session() as session:
        # import model to query by primary key
        from src.database.models import ApprovalRequest

        record = await session.get(ApprovalRequest, approval_id)
        if not record:
            raise HTTPException(status_code=404, detail="Approval not found")
        deployment = record.deployment
        target_payload = record.payload or {}
        approval_status = (record.status or "").upper()

    suggested_action = str(target_payload.get("suggested_action") or "UNKNOWN").strip().upper()
    auto_scaling_actions = {"SCALE_UP", "SCALE_DOWN", "SET_REPLICAS", "BUDGET_REALLOCATION"}

    if suggested_action not in auto_scaling_actions:
        return JSONResponse(
            status_code=200,
            content={
                "status": "manual_action_required",
                "approval_id": approval_id,
                "action": suggested_action,
                "message": "This approval requires manual execution (no automated action available).",
            },
        )

    reps = (payload or {}).get("replicas")
    if reps is None:
        reps = target_payload.get("suggested_replicas")
    if reps is None:
        raise HTTPException(status_code=400, detail="Missing replicas in payload and no suggested_replicas found")

    # Human-approved execution must be explicit and fail closed.
    if approval_status != "APPROVED":
        raise HTTPException(status_code=409, detail="Approval must be approved before execution")

    try:
        # Preferred flow: temporarily raise HPA maxReplicas, then scale Deployment.
        # Use MCP tools if available to keep actions auditable and reversible.
        duration_seconds = int((payload or {}).get("duration_seconds", 900))
        cpu_down_threshold = int((payload or {}).get("cpu_down_threshold", 50))

        temp_hpa_requested = False
        temp_hpa_confirmed = False

        if hasattr(runtime, "_tool_registry") and runtime._tool_registry.has("set_hpa_max_replicas_temporary"):
            temp_hpa_requested = True
            try:
                await runtime._tool_registry.call(
                    "set_hpa_max_replicas_temporary",
                    {
                        "deployment": deployment,
                        "max_replicas": int(reps),
                        "duration_seconds": duration_seconds,
                        "cpu_down_threshold": cpu_down_threshold,
                    },
                )
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Temporary HPA patch failed: {exc}")

            if hasattr(runtime, "_tool_registry") and runtime._tool_registry.has("get_hpa_limits"):
                for _attempt in range(3):
                    try:
                        res = await runtime._tool_registry.call("get_hpa_limits", {"deployment": deployment})
                        if isinstance(res, dict) and res.get("status") == "success":
                            hpa_max = res.get("data", {}).get("hpa_max_replicas")
                            if hpa_max == int(reps):
                                temp_hpa_confirmed = True
                                break
                    except Exception:
                        pass
                    await asyncio.sleep(2)

                if not temp_hpa_confirmed:
                    raise HTTPException(
                        status_code=500,
                        detail="Temporary HPA patch could not be confirmed; execution aborted",
                    )

        # perform direct scaling via Kubernetes client to apply replicas immediately
        from kubernetes import config as k8s_config, client as k8s_client

        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()

        apps_api = k8s_client.AppsV1Api()
        body = {"spec": {"replicas": int(reps)}}
        apps_api.patch_namespaced_deployment_scale(name=deployment, namespace="default", body=body)

        # mark approval as executed/completed in DB
        await update_approval_status(
            session,
            approval_id=approval_id,
            status="COMPLETED",
            approver=None,
            decision_at=datetime.now(timezone.utc),
            decision_reason="Executed by human-approved override",
        )

        # annotate incident report if present
        incident_id = (target_payload or {}).get("report", {}).get("incident_id")
        if incident_id:
            existing = await fetch_incident_report(session, incident_id) or {}
            existing.setdefault("execution_details", {})
            existing["execution_details"]["human_approval"] = "approved_executed"
            existing["execution_details"]["outcome"] = f"Scaled to {int(reps)} by human-approved override"
            await upsert_incident_report(
                session,
                incident_id=incident_id,
                timestamp_utc=datetime.now(timezone.utc),
                deployment=existing.get("alert_context", {}).get("deployment", "unknown"),
                report_data=existing,
            )

        # Start a background monitor task to periodically call check_and_revert_temp_hpa.
        # Only schedule this if the temporary HPA mechanism was actually used.
        if temp_hpa_requested and temp_hpa_confirmed:
            async def _monitor_and_revert(dep: str, interval: int = 30, timeout: int = 3600):
                started = datetime.now(timezone.utc)
                end_time = started.timestamp() + timeout
                while datetime.now(timezone.utc).timestamp() < end_time:
                    try:
                        if hasattr(runtime, "_tool_registry") and runtime._tool_registry.has("check_and_revert_temp_hpa"):
                            res = await runtime._tool_registry.call("check_and_revert_temp_hpa", {"deployment": dep})
                            data = res.get("data") if isinstance(res, dict) else None
                            if isinstance(data, list) and any(r.get("action") == "reverted" for r in data):
                                return
                        else:
                            return
                    except Exception:
                        pass
                    await asyncio.sleep(interval)

            asyncio.create_task(_monitor_and_revert(deployment, interval=30, timeout=duration_seconds + 600))

        return JSONResponse(status_code=200, content={"status": "executed", "result": {"status": "success", "deployment": deployment, "new_replicas": int(reps)}, "approval_id": approval_id})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Human-approved override failed: {exc}")

    # ensure runtime has k8s tool for non-approved automated execution
    if not hasattr(runtime, "_tool_registry") or not runtime._tool_registry.has("set_replicas"):
        raise HTTPException(status_code=500, detail="Scaling tool not available")

    try:
        result = await runtime._tool_registry.call("set_replicas", {"deployment": deployment, "replicas": int(reps)})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Tool call failed: {exc}")

    return JSONResponse(status_code=200, content={"status": "executed", "result": result, "approval_id": approval_id})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_agent_guardian:app", host="0.0.0.0", port=8000, log_level="info")
