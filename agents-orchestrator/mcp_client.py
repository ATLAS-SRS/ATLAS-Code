import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, TypedDict

from openai import AsyncOpenAI
from langgraph.graph import END, StateGraph
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client

from structured_logger import get_logger


LOGGER = get_logger("mcp-client")
LOGGER.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

LM_STUDIO_URL = os.getenv("LM_STUDIO_URL", "").strip()
LLM_API_URL = os.getenv("LLM_API_URL", "").strip() or LM_STUDIO_URL or "http://host.docker.internal:1234/v1"
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5-coder")
OPTIMIZE_CONTEXT = os.getenv("OPTIMIZE_CONTEXT", "auto").lower()
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "15"))
MAX_STEPS = int(os.getenv("MAX_TOOL_STEPS", "5"))
GRAFANA_MCP_URL = os.getenv(
    "GRAFANA_MCP_URL",
    "http://grafana-mcp-service.default.svc.cluster.local/sse",
).strip()
TARGET_DEPLOYMENTS = [
    deployment.strip()
    for deployment in os.getenv(
        "TARGET_DEPLOYMENTS",
        "api-gateway,scoring-system,enrichment-system,notification-system",
    ).split(",")
    if deployment.strip()
]

if not TARGET_DEPLOYMENTS:
    raise RuntimeError("TARGET_DEPLOYMENTS non può essere vuoto")

if not GRAFANA_MCP_URL:
    raise RuntimeError("GRAFANA_MCP_URL non può essere vuoto")


def _build_system_prompt(deployment: str) -> str:
    return f"""Sei un autoscaler autonomo di Kubernetes.
Il tuo obiettivo è mantenere stabile il deployment corrente: {deployment}.
I deployment target sono: {', '.join(TARGET_DEPLOYMENTS)}.
Regole operative:
1. Prima di prendere qualsiasi decisione di scaling, consulta i tool di osservabilità del server Grafana: usa i log di Loki e le metriche di Prometheus per capire il contesto operativo.
2. Poi usa get_scaling_recommendation per il deployment corrente.
3. Usa get_current_replicas per leggere lo stato attuale se necessario.
4. Se il traffico è basso e le repliche sono alte, DEVI usare set_replicas per fare SCALE DOWN.
5. Se il traffico è alto e le repliche sono poche, DEVI usare set_replicas per fare SCALE UP.
6. Se i log o le metriche mostrano errori, saturazione o regressioni applicative, considera prima quel segnale e spiega il ragionamento.
7. Se la situazione è stabile, non fare nulla (HOLD).
8. Non modificare deployment diversi da quello corrente.
9. I tool restituiscono un JSON con chiavi 'status', 'message' e 'data'. Se 'status' == 'error', i dati di monitoraggio sono inaffidabili: scrivi nel report l'errore e DEVI mantenere HOLD.
Produci sempre un breve riassunto testuale alla fine di ogni tuo intervento."""


def _build_user_prompt(deployment: str) -> str:
    return (
        f"Analizza il deployment '{deployment}' e le sue metriche. "
        "Se la raccomandazione indica scaling, applicala con lo stesso criterio "
        "usato per gli altri servizi del flusso."
    )


def _tool_to_openai_schema(tool, source: str) -> dict[str, Any]:
    """Converte un tool MCP nel formato richiesto dalle API OpenAI/LM Studio"""
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", {})
    description = (getattr(tool, "description", "") or "").strip()
    source_label = f"source: {source}"
    if description:
        description = f"{description} ({source_label})"
    else:
        description = source_label
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": description,
            "parameters": schema,
        },
    }


@dataclass
class ToolRegistry:
    _tool_sessions: dict[str, ClientSession] = field(default_factory=dict)
    _tool_sources: dict[str, str] = field(default_factory=dict)
    _openai_tools: list[dict[str, Any]] = field(default_factory=list)

    def register_tools(self, source: str, session: ClientSession, tools: list[Any]) -> None:
        for tool in tools:
            if tool.name in self._tool_sessions:
                previous_source = self._tool_sources[tool.name]
                raise RuntimeError(
                    f"Tool duplicato '{tool.name}' tra '{previous_source}' e '{source}'"
                )

            self._tool_sessions[tool.name] = session
            self._tool_sources[tool.name] = source
            self._openai_tools.append(_tool_to_openai_schema(tool, source))

    def source_for(self, tool_name: str) -> str | None:
        return self._tool_sources.get(tool_name)

    def tool_names(self) -> list[str]:
        return list(self._tool_sessions.keys())

    @property
    def openai_tools(self) -> list[dict[str, Any]]:
        return list(self._openai_tools)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        session = self._tool_sessions.get(tool_name)
        if session is None:
            raise KeyError(f"Tool '{tool_name}' non presente nel registry")

        return await session.call_tool(tool_name, arguments=arguments)


class AgentState(TypedDict, total=False):
    deployment: str
    messages: list[dict[str, Any]]
    step_count: int
    final_report: str
    last_error: str


def _build_initial_state(deployment: str) -> AgentState:
    return {
        "deployment": deployment,
        "messages": [
            {"role": "system", "content": _build_system_prompt(deployment)},
            {"role": "user", "content": _build_user_prompt(deployment)},
        ],
        "step_count": 0,
        "final_report": "",
        "last_error": "",
    }


def _tool_call_details(tool_call: Any) -> tuple[str, str, str]:
    if isinstance(tool_call, dict):
        function = tool_call.get("function") or {}
        return (
            tool_call.get("id", ""),
            function.get("name", ""),
            function.get("arguments", "") or "{}",
        )

    function = getattr(tool_call, "function", None)
    return (
        getattr(tool_call, "id", ""),
        getattr(function, "name", ""),
        getattr(function, "arguments", "") or "{}",
    )


def _format_tool_output(result: Any) -> dict[str, Any]:
    result_content = getattr(result, "content", []) or []
    text_result = " ".join([block.text for block in result_content if hasattr(block, "text")])
    parsed_result: Any = text_result
    if text_result:
        try:
            parsed_result = json.loads(text_result)
        except json.JSONDecodeError:
            parsed_result = text_result

    if isinstance(parsed_result, dict) and "status" in parsed_result:
        return parsed_result

    return {"status": "success", "message": "OK", "data": parsed_result}


def _build_agent_graph(
    tool_registry: ToolRegistry,
    llm_client: AsyncOpenAI,
    openai_tools: list[dict[str, Any]],
):
    async def llm_step(state: AgentState) -> dict[str, Any]:
        deployment = state["deployment"]
        messages = list(state["messages"])
        step_count = state.get("step_count", 0)

        if step_count >= MAX_STEPS:
            return {"final_report": "Raggiunto numero massimo di step. Operazione terminata per timeout."}

        try:
            response = await llm_client.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                tools=openai_tools,
                tool_choice="auto",
                temperature=0.0,
            )
        except Exception as exc:
            LOGGER.error(
                "Errore chiamata LLM",
                extra={"deployment": deployment, "step": step_count, "error": str(exc)},
            )
            return {
                "final_report": f"Errore durante la chiamata al modello LLM: {exc}. Mantengo HOLD per sicurezza.",
                "last_error": str(exc),
            }

        assistant_msg = response.choices[0].message
        message_dict = assistant_msg.model_dump(exclude_unset=True)
        tool_calls = message_dict.get("tool_calls") or []

        LOGGER.info(
            "Risposta LLM ricevuta",
            extra={
                "deployment": deployment,
                "step": step_count,
                "tool_calls": len(tool_calls),
                "has_content": bool(assistant_msg.content),
            },
        )

        return {
            "messages": messages + [message_dict],
            "final_report": assistant_msg.content or "",
            "last_error": "",
        }

    async def tool_step(state: AgentState) -> dict[str, Any]:
        deployment = state["deployment"]
        messages = list(state["messages"])
        assistant_msg = messages[-1] if messages else {}
        tool_calls = assistant_msg.get("tool_calls") or []

        if not tool_calls:
            return {"messages": messages}

        for tool_call in tool_calls:
            tool_call_id, tool_name, raw_arguments = _tool_call_details(tool_call)

            if not tool_name:
                tool_output = {"status": "error", "message": "Missing tool name", "data": None}
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": json.dumps(tool_output),
                    }
                )
                continue

            try:
                tool_args = json.loads(raw_arguments or "{}")
                if not isinstance(tool_args, dict):
                    raise TypeError("Tool arguments must be a JSON object")
            except json.JSONDecodeError:
                tool_output = {
                    "status": "error",
                    "message": "Invalid JSON arguments provided. Please correct the format and try again.",
                    "data": None,
                }
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": json.dumps(tool_output),
                    }
                )
                continue
            except TypeError as exc:
                tool_output = {"status": "error", "message": str(exc), "data": None}
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": json.dumps(tool_output),
                    }
                )
                continue

            try:
                tool_source = tool_registry.source_for(tool_name) or "unknown"
                LOGGER.info(
                    "Esecuzione Tool MCP",
                    extra={
                        "deployment": deployment,
                        "tool": tool_name,
                        "tool_source": tool_source,
                        "tool_args": tool_args,
                    },
                )
                result = await tool_registry.call_tool(tool_name, arguments=tool_args)
                tool_output = _format_tool_output(result)
            except Exception as exc:
                LOGGER.error(
                    "Errore esecuzione Tool",
                    extra={"deployment": deployment, "tool": tool_name, "error": str(exc)},
                )
                tool_output = {"status": "error", "message": str(exc), "data": None}

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps(tool_output),
                }
            )

        next_step_count = state.get("step_count", 0) + 1
        updates: AgentState = {
            "messages": messages,
            "step_count": next_step_count,
            "last_error": "",
        }
        if next_step_count >= MAX_STEPS:
            updates["final_report"] = "Raggiunto numero massimo di step. Operazione terminata per timeout."
        return updates

    def route_after_llm(state: AgentState) -> str:
        messages = state.get("messages", [])
        if not messages:
            return END

        last_message = messages[-1]
        tool_calls = last_message.get("tool_calls") or []
        if not tool_calls:
            return END

        if state.get("step_count", 0) >= MAX_STEPS:
            return END

        return "tool_step"

    def route_after_tool(state: AgentState) -> str:
        if state.get("step_count", 0) >= MAX_STEPS:
            return END
        return "llm_step"

    workflow = StateGraph(AgentState)
    workflow.add_node("llm_step", llm_step)
    workflow.add_node("tool_step", tool_step)
    workflow.set_entry_point("llm_step")
    workflow.add_conditional_edges("llm_step", route_after_llm, {"tool_step": "tool_step", END: END})
    workflow.add_conditional_edges("tool_step", route_after_tool, {"llm_step": "llm_step", END: END})
    return workflow.compile()


async def _run_agent_cycle(agent_graph: Any, deployment: str) -> str:
    """Gestisce il ciclo di ragionamento e chiamate agli strumenti con l'LLM"""
    final_state = await agent_graph.ainvoke(_build_initial_state(deployment))
    report = (final_state.get("final_report") or "").strip()

    if report:
        return report

    messages = final_state.get("messages") or []
    if messages:
        last_message = messages[-1]
        if isinstance(last_message, dict):
            content = (last_message.get("content") or "").strip()
            if content:
                return content

    return "Raggiunto numero massimo di step. Operazione terminata per timeout."


async def main():
    # Normalizza l'URL per la libreria OpenAI
    base_url = LLM_API_URL.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"

    llm_client = AsyncOpenAI(base_url=base_url, api_key="local-no-key")

    # Questo è il cuore di MCP locale: il Client esegue il file del Server come sottoprocesso!
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-u", "mcp_server.py"],
        env=os.environ.copy(),
    )

    LOGGER.info(
        "Avvio MCP AIOps Controller. In attesa del primo ciclo...",
        extra={"target_deployments": TARGET_DEPLOYMENTS, "grafana_mcp_url": GRAFANA_MCP_URL},
    )

    while True:
        try:
            async with AsyncExitStack() as stack:
                stdio_transport = await stack.enter_async_context(stdio_client(server_params))
                scaling_session = await stack.enter_async_context(ClientSession(*stdio_transport))
                await scaling_session.initialize()

                grafana_transport = await stack.enter_async_context(sse_client(GRAFANA_MCP_URL))
                grafana_session = await stack.enter_async_context(ClientSession(*grafana_transport))
                await grafana_session.initialize()

                tool_registry = ToolRegistry()

                scaling_tools = await scaling_session.list_tools()
                tool_registry.register_tools("mcp_server.py", scaling_session, list(scaling_tools.tools))

                grafana_tools = await grafana_session.list_tools()

                is_small_model = any(keyword in LLM_MODEL.lower() for keyword in ["gemma", "3b", "7b", "8b", "qwen2.5-coder"])

                if OPTIMIZE_CONTEXT == "true":
                    should_filter = True
                elif OPTIMIZE_CONTEXT == "false":
                    should_filter = False
                else:
                    should_filter = is_small_model

                if should_filter:
                    ALLOWED_GRAFANA_TOOLS = {"query_prometheus", "query_loki_logs"}
                    tools_to_load = [t for t in grafana_tools.tools if t.name in ALLOWED_GRAFANA_TOOLS]
                    LOGGER.info(
                        "Modalità contesto ridotto: caricamento parziale tool Grafana", 
                        extra={"model": LLM_MODEL, "loaded_tools": len(tools_to_load)}
                    )
                else:
                    tools_to_load = list(grafana_tools.tools)
                    LOGGER.info(
                        "Modalità contesto completo: caricamento totale tool Grafana", 
                        extra={"model": LLM_MODEL, "loaded_tools": len(tools_to_load)}
                    )


                tool_registry.register_tools(
                    "grafana-mcp-service",
                    grafana_session,
                    tools_to_load,
                )

                openai_tools = tool_registry.openai_tools
                agent_graph = _build_agent_graph(tool_registry, llm_client, openai_tools)

                LOGGER.info(
                    "Connessioni MCP inizializzate",
                    extra={
                        "tools_caricati": tool_registry.tool_names(),
                        "tool_sources": {name: tool_registry.source_for(name) for name in tool_registry.tool_names()},
                        "target_deployments": TARGET_DEPLOYMENTS,
                    },
                )

                while True:
                    LOGGER.info(
                        "Inizio ciclo di analisi",
                        extra={"target_deployments": TARGET_DEPLOYMENTS},
                    )

                    for deployment in TARGET_DEPLOYMENTS:
                        try:
                            report = await _run_agent_cycle(
                                agent_graph,
                                deployment,
                            )
                            LOGGER.info(
                                "Ciclo concluso",
                                extra={"deployment": deployment, "decisione_llm": report},
                            )
                        except Exception as exc:
                            LOGGER.error(
                                "Errore durante analisi deployment",
                                extra={"deployment": deployment, "error": str(exc)},
                            )
                            continue

                    await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            LOGGER.error(
                "Connessione MCP interrotta, tentativo di riconnessione",
                extra={"error": str(e), "retry_in_seconds": 5},
            )
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())