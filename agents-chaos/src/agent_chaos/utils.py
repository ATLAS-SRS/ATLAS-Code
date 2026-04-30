import json
from typing import Any
from langchain_core.tools import StructuredTool
from src.agent_chaos.config import TARGET_DEPLOYMENTS

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

    deployment = deployment_hint.strip()
    if deployment and deployment not in TARGET_DEPLOYMENTS:
        maybe_name = deployment.split("-")[0]
        if maybe_name in TARGET_DEPLOYMENTS:
            deployment = maybe_name

    return {
        "alert_name": labels.get("alertname", "unknown"),
        "severity": labels.get("severity", "unknown"),
        "summary": annotations.get("summary", ""),
        "description": annotations.get("description", ""),
        "status": alert.get("status", "unknown"),
        "deployment": deployment,
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
