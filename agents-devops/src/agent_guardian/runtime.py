import os
import sys
import json
from contextlib import AsyncExitStack
from typing import Any
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI
from langchain_core.tools import StructuredTool
from langgraph.graph import END, StateGraph

from .config import (
    LOGGER, LLM_API_URL, LLM_API_KEY, LLM_MODEL,
    _normalize_llm_base_url, _resolve_k8s_mcp_script, MAX_TOOL_STEPS
)
from .registry import ToolRegistry, ToolDef
from .utils import (
    _structured_tool_schema, _invoke_langchain_tool,
    _parse_tool_result, _extract_alert_fields, _safe_json
)
from .prompts import _grafana_prompt, _reasoning_prompt
from .llm import _run_llm_tool_loop
from .state import AgentState

try:
    from src.agents.tools.grafana_mcp import GrafanaMCPManager
except ImportError:
    from src.tools.grafana_mcp import GrafanaMCPManager

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
        try:
            grafana_tools = await self._grafana_manager.get_tools(LLM_MODEL)
        except Exception as exc:
            LOGGER.error(
                "Grafana MCP unavailable at startup; continuing without Grafana tools",
                extra={"error": str(exc)},
            )
            return

        for tool in grafana_tools:
            # Estraiamo lo schema reale dal server MCP di Grafana
            parameters = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}

            # Creiamo la closure senza kwargs
            async def _invoke(args: dict[str, Any], t_name: str = tool.name) -> dict[str, Any]:
                try:
                    raw_str = await self._grafana_manager.execute_tool(t_name, args)
                    return json.loads(raw_str)
                except Exception as exc:
                    return {"status": "error", "message": str(exc), "data": None}

            self._tool_registry.register(
                ToolDef(
                    name=tool.name,
                    description=tool.description or "Grafana MCP tool",
                    parameters=parameters,
                    source="grafana_mcp",
                    invoke=_invoke,
                )
            )

        LOGGER.info(
            "Grafana tools loaded",
            extra={"tools": [t.name for t in grafana_tools]},
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
                if name in {"query_prometheus", "query_loki_logs", "list_datasources"}
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
