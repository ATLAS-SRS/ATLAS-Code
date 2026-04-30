import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, create_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from .config import (
    LOGGER,
)
from .llm import call_litellm_with_tools, create_litellm_client
from .prompts import CHAOS_SYSTEM_PROMPT
from .state import ChaosAgentState


def _parse_mcp_result(result: Any) -> dict[str, Any]:
    blocks = getattr(result, "content", []) or []
    text_payload = " ".join([block.text for block in blocks if hasattr(block, "text")])
    if not text_payload:
        return {"status": "success", "message": "OK", "data": None}

    try:
        parsed = json.loads(text_payload)
    except json.JSONDecodeError:
        parsed = text_payload

    if isinstance(parsed, dict):
        return parsed

    return {"status": "success", "message": "OK", "data": parsed}


def _resolve_chaos_mcp_script() -> str:
    override = os.getenv("CHAOS_MCP_SERVER_SCRIPT", "").strip()
    base_dir = Path(__file__).resolve().parents[2]

    if override:
        candidate = Path(override)
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        if candidate.exists():
            return str(candidate)
        raise RuntimeError(f"Configured CHAOS_MCP_SERVER_SCRIPT not found: {candidate}")

    default_script = base_dir / "chaos_mcp.py"
    if default_script.exists():
        return str(default_script)

    raise RuntimeError("chaos_mcp.py was not found in agents-chaos root")


def _schema_to_args_model(model_name: str, schema: dict[str, Any]) -> type[BaseModel]:
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    fields: dict[str, tuple[Any, Any]] = {}

    for key, prop in properties.items():
        json_type = (prop or {}).get("type", "string")
        description = (prop or {}).get("description", "")

        if json_type == "integer":
            py_type = int
        elif json_type == "number":
            py_type = float
        elif json_type == "boolean":
            py_type = bool
        elif json_type == "array":
            py_type = list
        elif json_type == "object":
            py_type = dict
        else:
            py_type = str

        if key in required:
            fields[key] = (py_type, Field(..., description=description))
        else:
            fields[key] = (py_type | None, Field(default=None, description=description))

    if not fields:
        fields["dummy_arg"] = (Optional[str], Field(default=None, description="Dummy argument for zero-param tools"))

    return create_model(model_name, **fields)


class ChaosAgentRuntime:
    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._llm_client: AsyncOpenAI | None = None
        self._mcp_session: ClientSession | None = None
        self._graph = None
        self._openai_tool_schemas: list[dict[str, Any]] = []
        self._langchain_tools: list[StructuredTool] = []

    @property
    def graph(self):
        if self._graph is None:
            raise RuntimeError("Chaos graph is not initialized")
        return self._graph

    async def start(self) -> None:
        self._llm_client = create_litellm_client()

        await self._connect_mcp_server()
        self._graph = self._build_graph()

        LOGGER.info(
            "Chaos agent runtime started",
            extra={
                "registered_tools": [t.name for t in self._langchain_tools],
            },
        )

    async def stop(self) -> None:
        await self._stack.aclose()
        LOGGER.info("Chaos agent runtime stopped")

    async def _connect_mcp_server(self) -> None:
        script = _resolve_chaos_mcp_script()
        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-u", script],
            env=os.environ.copy(),
        )

        transport = await self._stack.enter_async_context(stdio_client(server_params))
        session = await self._stack.enter_async_context(ClientSession(*transport))
        await session.initialize()

        listed = await session.list_tools()
        loaded_tools: list[StructuredTool] = []
        schemas: list[dict[str, Any]] = []

        for tool in listed.tools:
            name = tool.name
            description = tool.description or "Chaos MCP tool"
            input_schema = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}
            args_schema = _schema_to_args_model(f"{name.title().replace('_', '')}Args", input_schema)

            async def _invoke_tool(
                session_ref: ClientSession = session,
                tool_name: str = name,
                **kwargs: Any,
            ) -> dict[str, Any]:
                payload = {k: v for k, v in kwargs.items() if k != "_noop" and v is not None}
                raw = await session_ref.call_tool(tool_name, arguments=payload)
                return _parse_mcp_result(raw)

            loaded_tools.append(
                StructuredTool.from_function(
                    coroutine=_invoke_tool,
                    name=name,
                    description=description,
                    args_schema=args_schema,
                )
            )
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": input_schema,
                    },
                }
            )

        self._mcp_session = session
        self._langchain_tools = loaded_tools
        self._openai_tool_schemas = schemas

        LOGGER.info(
            "Chaos MCP tools connected",
            extra={"script": script, "tools": [t.name for t in loaded_tools]},
        )

    async def _reasoning_and_action(self, state: ChaosAgentState) -> dict[str, Any]:
        llm_client = self._llm_client
        if llm_client is None:
            raise RuntimeError("LLM client is not initialized")

        existing_messages = list(state.get("messages") or [])
        has_system_prompt = any(isinstance(msg, SystemMessage) for msg in existing_messages)
        if not has_system_prompt:
            existing_messages.insert(0, SystemMessage(content=CHAOS_SYSTEM_PROMPT))

        if not existing_messages:
            existing_messages = [
                SystemMessage(content=CHAOS_SYSTEM_PROMPT),
                HumanMessage(
                    content=(
                        "Start a controlled chaos experiment now. "
                        "Follow Reconnaissance -> Plan -> Execute -> Report and end with a formal Markdown report."
                    )
                ),
            ]

        openai_messages: list[dict[str, Any]] = []
        for msg in existing_messages:
            if isinstance(msg, SystemMessage):
                openai_messages.append({"role": "system", "content": msg.content})
            elif isinstance(msg, HumanMessage):
                openai_messages.append({"role": "user", "content": msg.content})
            elif isinstance(msg, ToolMessage):
                openai_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id,
                        "content": msg.content,
                    }
                )
            elif isinstance(msg, AIMessage):
                asst: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
                if msg.tool_calls:
                    asst["tool_calls"] = [
                        {
                            "id": tc.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": tc.get("name", ""),
                                "arguments": json.dumps(tc.get("args", {}), ensure_ascii=True),
                            },
                        }
                        for tc in msg.tool_calls
                    ]
                openai_messages.append(asst)

        model_output = await call_litellm_with_tools(
            llm_client=llm_client,
            messages=openai_messages,
            tool_schemas=self._openai_tool_schemas,
        )
        tool_calls_payload = model_output.get("tool_calls") or []

        ai_message = AIMessage(
            content=str(model_output.get("content") or ""),
            tool_calls=tool_calls_payload,
        )

        update: dict[str, Any] = {"messages": [ai_message]}
        if not tool_calls_payload:
            update["execution_report"] = str(model_output.get("content") or "").strip()

        LOGGER.info(
            "Reasoning step complete",
            extra={
                "tool_calls": [tc.get("name") for tc in tool_calls_payload],
                "has_report": not bool(tool_calls_payload),
            },
        )
        return update

    def _route_after_reasoning(self, state: ChaosAgentState) -> str:
        messages = state.get("messages") or []
        if not messages:
            return END

        last = messages[-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return END

    def _build_graph(self):
        if not self._langchain_tools:
            raise RuntimeError("No MCP tools available for chaos execution")

        workflow = StateGraph(ChaosAgentState)
        workflow.add_node("reasoning_and_action", self._reasoning_and_action)
        workflow.add_node("tools", ToolNode(self._langchain_tools))

        workflow.set_entry_point("reasoning_and_action")
        workflow.add_conditional_edges(
            "reasoning_and_action",
            self._route_after_reasoning,
            {
                "tools": "tools",
                END: END,
            },
        )
        workflow.add_edge("tools", "reasoning_and_action")
        return workflow.compile()
