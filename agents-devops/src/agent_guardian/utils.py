import json
from typing import Any

try:
    from langchain_core.tools import StructuredTool
except ImportError:  # pragma: no cover - fallback for lightweight unit tests
    StructuredTool = Any

from src.agent_guardian.config import (
    AUTOSCALABLE_DEPLOYMENTS,
    MONITORED_ONLY_WORKLOADS,
    MONITORED_WORKLOADS,
)

def _safe_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=True, default=str)

def _parse_tool_result(result: Any) -> dict[str, Any]:
    blocks = getattr(result, "content", []) or []
    text_payload = " ".join([block.text for block in blocks if hasattr(block, "text")])
    if not text_payload:
        return {"status": "success", "message": "OK", "data": None}

    try:
        parsed = json.loads(text_payload)
    except json.JSONDecodeError:
        parsed = text_payload

    if isinstance(parsed, dict) and "status" in parsed:
        return parsed

    return {"status": "success", "message": "OK", "data": parsed}

def _tool_call_parts(tool_call: Any) -> tuple[str, str, str]:
    if isinstance(tool_call, dict):
        function = tool_call.get("function") or {}
        return (
            tool_call.get("id", ""),
            function.get("name", ""),
            function.get("arguments", "{}") or "{}"
        )

    fn = getattr(tool_call, "function", None)
    return (
        getattr(tool_call, "id", ""),
        getattr(fn, "name", ""),
        getattr(fn, "arguments", "{}") or "{}"
    )

def _normalize_workload_name(workload_hint: str) -> str:
    raw_hint = (workload_hint or "").strip()
    if not raw_hint:
        return ""

    candidates = [raw_hint.lower()]
    if "/" in candidates[0]:
        candidates.append(candidates[0].rsplit("/", 1)[-1])

    alias_map = {
        "postgresql": "atlas-postgres-postgresql",
        "atlas-postgres": "atlas-postgres-postgresql",
        "redis": "atlas-redis-master",
        "atlas-redis": "atlas-redis-master",
    }

    for candidate in candidates:
        if candidate in alias_map:
            return alias_map[candidate]

    for candidate in candidates:
        for workload in sorted(MONITORED_WORKLOADS, key=len, reverse=True):
            if (
                candidate == workload
                or candidate.startswith(f"{workload}-")
                or candidate.startswith(f"{workload}.")
                or candidate.startswith(f"{workload}_")
            ):
                return workload

    for candidate in candidates:
        if candidate in MONITORED_WORKLOADS:
            return candidate

    return raw_hint

def _classify_workload(workload_hint: str) -> tuple[str, str]:
    workload = _normalize_workload_name(workload_hint)
    if workload in AUTOSCALABLE_DEPLOYMENTS:
        return workload, "AUTOSCALE"
    if workload in MONITORED_ONLY_WORKLOADS:
        return workload, "MONITOR_ONLY"
    return workload, "UNMONITORED"

def _extract_alert_fields(alert: dict[str, Any]) -> dict[str, Any]:
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}

    deployment_hint = (
        labels.get("deployment")
        or labels.get("service")
        or labels.get("app")
        or labels.get("pod")
        or ""
    )

    deployment, workload_policy = _classify_workload(deployment_hint)

    return {
        "alert_name": labels.get("alertname", "unknown"),
        "severity": labels.get("severity", "unknown"),
        "summary": annotations.get("summary", ""),
        "description": annotations.get("description", ""),
        "status": alert.get("status", "unknown"),
        "deployment": deployment,
        "workload_policy": workload_policy,
        "labels": labels,
        "startsAt": alert.get("startsAt", ""),
        "endsAt": alert.get("endsAt", ""),
    }

def _structured_tool_schema(tool: StructuredTool) -> dict[str, Any]:
    args_schema = getattr(tool, "args_schema", None)
    if args_schema is None:
        return {"type": "object", "properties": {}, "additionalProperties": True}

    try:
        schema = args_schema.model_json_schema()
        if isinstance(schema, dict):
            return schema
    except Exception:
        pass

    return {"type": "object", "properties": {}, "additionalProperties": True}

async def _invoke_langchain_tool(tool: StructuredTool, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        if hasattr(tool, "ainvoke"):
            raw = await tool.ainvoke(arguments)
        else:
            raw = tool.invoke(arguments)

        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
                return {"status": "success", "message": "OK", "data": parsed}
            except json.JSONDecodeError:
                return {"status": "success", "message": "OK", "data": raw}

        if isinstance(raw, dict):
            return raw

        return {"status": "success", "message": "OK", "data": raw}
    except Exception as exc:
        return {"status": "error", "message": str(exc), "data": None}


def _clean_json_markdown(text: str) -> str:
    text = text.strip()
    if text.startswith('```json'):
        text = text[7:].strip()
    elif text.startswith('```'):
        text = text[3:].strip()
    if text.endswith('```'):
        text = text[:-3].strip()
    return text
