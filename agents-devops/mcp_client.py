#!/usr/bin/env python3
import json
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, TypedDict

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from langchain_core.tools import StructuredTool
from langgraph.graph import END, StateGraph
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI

from structured_logger import get_logger

try:
    # Preferred path requested by architecture docs.
    from src.agents.tools.grafana_mcp import GrafanaMCPManager
except ImportError:
    # Fallback for current repository layout.
    from src.tools.grafana_mcp import GrafanaMCPManager


LOGGER = get_logger("sre-guardian", stream=sys.stderr)
LOGGER.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5-coder").strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", "local-no-key").strip()
LLM_API_URL = (
    os.getenv("LLM_API_URL", "").strip()
    or os.getenv("LM_STUDIO_URL", "").strip()
    or "http://host.docker.internal:1234/v1"
)
MAX_TOOL_STEPS = int(os.getenv("MAX_TOOL_STEPS", "6"))
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "90"))

DEFAULT_TARGET_DEPLOYMENTS = "api-gateway,scoring-system,enrichment-system,notification-system"
TARGET_DEPLOYMENTS = {
    item.strip()
    for item in os.getenv("TARGET_DEPLOYMENTS", DEFAULT_TARGET_DEPLOYMENTS).split(",")
    if item.strip()
}


def _normalize_llm_base_url(url: str) -> str:
    clean = url.rstrip("/")
    if not clean.endswith("/v1"):
        clean = f"{clean}/v1"
    return clean


def _resolve_k8s_mcp_script() -> str:
    override = os.getenv("K8S_MCP_SERVER_SCRIPT", "").strip()
    base_dir = Path(__file__).resolve().parent

    if override:
        candidate = Path(override)
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        if candidate.exists():
            return str(candidate)
        raise RuntimeError(f"Configured K8S_MCP_SERVER_SCRIPT not found: {candidate}")

    primary = base_dir / "mcp_server.py"
    if primary.exists():
        return str(primary)

    fallback = base_dir / "k8s_mcp.py"
    if fallback.exists():
        LOGGER.info(
            "mcp_server.py not found, using k8s_mcp.py fallback",
            extra={"script": str(fallback)},
        )
        return str(fallback)

    raise RuntimeError("Neither mcp_server.py nor k8s_mcp.py was found in agents-devops/")


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
            function.get("arguments", "{}") or "{}",
        )

    fn = getattr(tool_call, "function", None)
    return (
        getattr(tool_call, "id", ""),
        getattr(fn, "name", ""),
        getattr(fn, "arguments", "{}") or "{}",
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
        # Try simple pod prefix mapping: foo-7f66d5 -> foo
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


def _grafana_prompt(parsed_alert: dict[str, Any], full_alert: dict[str, Any]) -> tuple[str, str]:
    system_prompt = (
        "You are SRE Guardian for the ATLAS platform. "
        "You received a production alert. "
        "Investigate only with Grafana MCP read-only tools (Prometheus and Loki). "
        "Determine whether resource pressure is caused by legitimate traffic growth or by an application bug "
        "(for example errors, retry storms, infinite loops, or failing downstream dependencies). "
        "If evidence is insufficient, state UNCLEAR and explain what data is missing. "
        "Never suggest direct Kubernetes actions in this phase. "
        "Always return a concise JSON object with fields: "
        "verdict (TRAFFIC|BUG|UNCLEAR), confidence (0..1), evidence (array of strings), "
        "recommended_next_step (string)."
    )

    user_prompt = (
        "Alert payload (single firing alert):\n"
        f"{_safe_json(full_alert)}\n\n"
        "Normalized fields:\n"
        f"{_safe_json(parsed_alert)}\n\n"
        "Use Grafana tools now."
    )
    return system_prompt, user_prompt


def _reasoning_prompt(
    parsed_alert: dict[str, Any],
    full_alert: dict[str, Any],
    investigation_report: str,
) -> tuple[str, str]:
    system_prompt = (
        "You are SRE Guardian. Decide and execute safe remediation through Kubernetes MCP tools only. "
        "Inputs include Alertmanager data and the investigation report. "
        "Policy: if verdict indicates real traffic increase, you may scale up using set_replicas. "
        "If verdict indicates BUG/ERROR behavior, do NOT scale up; produce rollback recommendation and incident notes. "
        "If uncertainty remains, keep HOLD and explain. "
        "K8s MCP enforces MIN/MAX replicas and can reject unsafe actions; handle tool errors explicitly. "
        "Always call get_current_replicas before deciding a replica change. "
        "Return final response as JSON with fields: action (SCALE_UP|HOLD|ROLLBACK_RECOMMENDATION), "
        "deployment, rationale, executed_tools (array), outcome, follow_up."
    )

    user_prompt = (
        "Alert payload:\n"
        f"{_safe_json(full_alert)}\n\n"
        "Normalized fields:\n"
        f"{_safe_json(parsed_alert)}\n\n"
        "Investigation report from Grafana phase:\n"
        f"{investigation_report}\n\n"
        "Execute reasoning and action now via K8s MCP tools if needed."
    )
    return system_prompt, user_prompt


class AgentState(TypedDict, total=False):
    alert: dict[str, Any]
    parsed_alert: dict[str, Any]
    investigation_report: str
    final_report: str
    llm_trace: list[dict[str, Any]]
    error: str


ToolCallable = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]
    source: str
    invoke: ToolCallable


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool_def: ToolDef) -> None:
        if tool_def.name in self._tools:
            raise RuntimeError(f"Duplicate tool registration for {tool_def.name}")
        self._tools[tool_def.name] = tool_def

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def has(self, name: str) -> bool:
        return name in self._tools

    def select_openai_schemas(self, allowed_names: set[str]) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for name, tool_def in self._tools.items():
            if name not in allowed_names:
                continue
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": f"{tool_def.description} (source: {tool_def.source})",
                        "parameters": tool_def.parameters,
                    },
                }
            )
        return schemas

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool_def = self._tools.get(tool_name)
        if tool_def is None:
            return {
                "status": "error",
                "message": f"Requested tool not found: {tool_name}",
                "data": None,
            }
        return await tool_def.invoke(arguments)


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


async def _run_llm_tool_loop(
    *,
    llm_client: AsyncOpenAI,
    model: str,
    tool_registry: ToolRegistry,
    allowed_tools: set[str],
    system_prompt: str,
    user_prompt: str,
    max_steps: int,
    trace_key: str,
    deployment: str,
) -> tuple[str, list[dict[str, Any]]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    schemas = tool_registry.select_openai_schemas(allowed_tools)

    for step in range(max_steps):
        LOGGER.info(
            "LLM reasoning step",
            extra={
                "phase": trace_key,
                "deployment": deployment,
                "step": step,
                "allowed_tools": sorted(list(allowed_tools)),
            },
        )

        response = await llm_client.chat.completions.create(
            model=model,
            messages=messages,
            tools=schemas,
            tool_choice="auto",
            temperature=0.0,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

        assistant_msg = response.choices[0].message
        msg_dict = assistant_msg.model_dump(exclude_unset=True)
        messages.append(msg_dict)

        tool_calls = msg_dict.get("tool_calls") or []
        if not tool_calls:
            return (assistant_msg.content or "", messages)

        for tool_call in tool_calls:
            tool_call_id, tool_name, raw_args = _tool_call_parts(tool_call)

            if tool_name not in allowed_tools:
                tool_output = {
                    "status": "error",
                    "message": f"Tool {tool_name} is not allowed in phase {trace_key}",
                    "data": None,
                }
            else:
                try:
                    parsed_args = json.loads(raw_args or "{}")
                    if not isinstance(parsed_args, dict):
                        raise TypeError("Tool arguments must be a JSON object")
                except Exception as exc:
                    parsed_args = {}
                    tool_output = {
                        "status": "error",
                        "message": f"Invalid tool arguments: {exc}",
                        "data": None,
                    }
                else:
                    LOGGER.info(
                        "Executing MCP tool",
                        extra={
                            "phase": trace_key,
                            "deployment": deployment,
                            "tool_name": tool_name,
                            "tool_args": parsed_args,
                        },
                    )
                    tool_output = await tool_registry.call(tool_name, parsed_args)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": _safe_json(tool_output),
                }
            )

    return (
        "{\"action\":\"HOLD\",\"rationale\":\"Max reasoning steps reached\",\"outcome\":\"No action\"}",
        messages,
    )


class SREGuardianRuntime:
    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._llm_client: AsyncOpenAI | None = None
        self._graph = None
        self._tool_registry = ToolRegistry()
        self._grafana_manager: GrafanaMCPManager | None = None

    @property
    def graph(self):
        if self._graph is None:
            raise RuntimeError("Runtime graph not initialized")
        return self._graph

    async def start(self) -> None:
        llm_base_url = _normalize_llm_base_url(LLM_API_URL)
        self._llm_client = AsyncOpenAI(base_url=llm_base_url, api_key=LLM_API_KEY)

        LOGGER.info(
            "Starting SRE Guardian runtime",
            extra={"llm_model": LLM_MODEL, "llm_base_url": llm_base_url},
        )

        await self._register_grafana_tools()
        await self._register_k8s_tools_stdio()
        self._graph = self._build_graph()

        LOGGER.info(
            "SRE Guardian runtime ready",
            extra={"registered_tools": sorted(self._tool_registry.names())},
        )

    async def stop(self) -> None:
        if self._grafana_manager is not None:
            try:
                await self._grafana_manager.close()
            except Exception as exc:
                LOGGER.error("Error closing Grafana manager", extra={"error": str(exc)})

        await self._stack.aclose()
        LOGGER.info("SRE Guardian runtime stopped")

    async def _register_grafana_tools(self) -> None:
        self._grafana_manager = GrafanaMCPManager()
        grafana_tools = await self._grafana_manager.get_tools(LLM_MODEL)

        # get_tools can emit duplicates with the same name in current implementation.
        unique_by_name: dict[str, StructuredTool] = {}
        for tool in grafana_tools:
            unique_by_name[tool.name] = tool

        for tool_name, tool in unique_by_name.items():
            self._tool_registry.register(
                ToolDef(
                    name=tool_name,
                    description=tool.description or "Grafana MCP tool",
                    parameters=_structured_tool_schema(tool),
                    source="grafana_mcp",
                    invoke=lambda args, t=tool: _invoke_langchain_tool(t, args),
                )
            )

        LOGGER.info(
            "Grafana tools loaded",
            extra={"tools": sorted(list(unique_by_name.keys()))},
        )

    async def _register_k8s_tools_stdio(self) -> None:
        script = _resolve_k8s_mcp_script()
        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-u", script],
            env=os.environ.copy(),
        )

        transport = await self._stack.enter_async_context(stdio_client(server_params))
        session = await self._stack.enter_async_context(ClientSession(*transport))
        await session.initialize()

        listed = await session.list_tools()
        for tool in listed.tools:
            name = tool.name
            description = tool.description or "Kubernetes MCP tool"
            parameters = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}

            async def _invoke(args: dict[str, Any], *, session_ref: ClientSession = session, tool_name: str = name) -> dict[str, Any]:
                try:
                    raw = await session_ref.call_tool(tool_name, arguments=args)
                    return _parse_tool_result(raw)
                except Exception as exc:
                    return {"status": "error", "message": str(exc), "data": None}

            self._tool_registry.register(
                ToolDef(
                    name=name,
                    description=description,
                    parameters=parameters,
                    source="k8s_stdio_mcp",
                    invoke=_invoke,
                )
            )

        LOGGER.info(
            "K8s MCP tools loaded",
            extra={"tools": [t.name for t in listed.tools], "script": script},
        )

    def _build_graph(self):
        llm_client = self._llm_client
        if llm_client is None:
            raise RuntimeError("LLM client not initialized")

        tool_registry = self._tool_registry

        async def receive_alert_node(state: AgentState) -> dict[str, Any]:
            raw_alert = state.get("alert") or {}
            parsed = _extract_alert_fields(raw_alert)

            LOGGER.info(
                "Alert received",
                extra={
                    "alert_name": parsed.get("alert_name", "unknown"),
                    "severity": parsed.get("severity", "unknown"),
                    "deployment": parsed.get("deployment", ""),
                    "status": parsed.get("status", "unknown"),
                },
            )

            return {"parsed_alert": parsed}

        async def investigate_node(state: AgentState) -> dict[str, Any]:
            parsed_alert = state.get("parsed_alert") or {}
            full_alert = state.get("alert") or {}
            deployment = parsed_alert.get("deployment", "")

            grafana_allowed = {
                name
                for name in tool_registry.names()
                if name in {"query_prometheus", "query_loki_logs"}
            }

            if not grafana_allowed:
                msg = "No Grafana tools available; cannot investigate telemetry evidence"
                LOGGER.error(msg)
                return {"investigation_report": msg, "error": msg}

            system_prompt, user_prompt = _grafana_prompt(parsed_alert, full_alert)
            report, trace = await _run_llm_tool_loop(
                llm_client=llm_client,
                model=LLM_MODEL,
                tool_registry=tool_registry,
                allowed_tools=grafana_allowed,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_steps=MAX_TOOL_STEPS,
                trace_key="investigate",
                deployment=deployment,
            )

            LOGGER.info(
                "Investigation completed",
                extra={"deployment": deployment, "report": report[:500]},
            )
            return {"investigation_report": report, "llm_trace": trace}

        async def reasoning_action_node(state: AgentState) -> dict[str, Any]:
            parsed_alert = state.get("parsed_alert") or {}
            full_alert = state.get("alert") or {}
            investigation_report = state.get("investigation_report", "")
            deployment = parsed_alert.get("deployment", "")

            if not deployment:
                report = _safe_json(
                    {
                        "action": "HOLD",
                        "deployment": "",
                        "rationale": "No deployment/service label could be mapped from the alert payload",
                        "executed_tools": [],
                        "outcome": "No scaling action",
                        "follow_up": "Add deployment or service labels to alert rules",
                    }
                )
                return {"final_report": report}

            k8s_allowed = {
                name
                for name in tool_registry.names()
                if name in {"get_current_replicas", "set_replicas"}
            }

            if not k8s_allowed:
                msg = _safe_json(
                    {
                        "action": "HOLD",
                        "deployment": deployment,
                        "rationale": "No K8s MCP action tools available",
                        "executed_tools": [],
                        "outcome": "No scaling action",
                        "follow_up": "Validate stdio connection to k8s MCP server",
                    }
                )
                return {"final_report": msg, "error": "missing_k8s_tools"}

            system_prompt, user_prompt = _reasoning_prompt(parsed_alert, full_alert, investigation_report)
            final_report, trace = await _run_llm_tool_loop(
                llm_client=llm_client,
                model=LLM_MODEL,
                tool_registry=tool_registry,
                allowed_tools=k8s_allowed,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_steps=MAX_TOOL_STEPS,
                trace_key="reasoning_action",
                deployment=deployment,
            )

            LOGGER.info(
                "Reasoning and action completed",
                extra={"deployment": deployment, "final_report": final_report[:500]},
            )

            merged_trace = list(state.get("llm_trace") or []) + trace
            return {"final_report": final_report, "llm_trace": merged_trace}

        workflow = StateGraph(AgentState)
        workflow.add_node("ReceiveAlert", receive_alert_node)
        workflow.add_node("Investigate", investigate_node)
        workflow.add_node("ReasoningAction", reasoning_action_node)

        workflow.set_entry_point("ReceiveAlert")
        workflow.add_edge("ReceiveAlert", "Investigate")
        workflow.add_edge("Investigate", "ReasoningAction")
        workflow.add_edge("ReasoningAction", END)

        return workflow.compile()


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

    uvicorn.run("mcp_client:app", host="0.0.0.0", port=8000, log_level="info")
