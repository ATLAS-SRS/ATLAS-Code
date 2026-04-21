import os
import json
from typing import Any, Awaitable, Callable

from mcp import ClientSession
from mcp.client.sse import sse_client
from langchain_core.tools import StructuredTool
from structured_logger import get_logger

LOGGER = get_logger("grafana-mcp-tools")


class GrafanaMCPManager:
    """Manages the SSE connection to Grafana MCP and provides LangChain tools."""

    def __init__(self):
        namespace = os.getenv("NAMESPACE", "default").strip() or "default"
        self.mcp_url = os.getenv(
            "GRAFANA_MCP_URL",
            f"http://grafana-mcp-service.{namespace}.svc.cluster.local:80/sse"
        ).strip()
        self.optimize_context = os.getenv("OPTIMIZE_CONTEXT", "auto").lower()
        self.allowed_tools = {"query_prometheus", "query_loki_logs"}

        self._tools_cache: list[StructuredTool] | None = None

    async def _with_session(self, operation: Callable[[ClientSession], Awaitable[Any]]) -> Any:
        """Open and close SSE/ClientSession in the same task."""
        LOGGER.info(f"Connecting to Grafana MCP at {self.mcp_url}")
        async with sse_client(self.mcp_url) as transport:
            async with ClientSession(*transport) as session:
                await session.initialize()
                return await operation(session)

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Executes a tool on the remote MCP server and formats the output."""
        try:
            async def _call(session: ClientSession):
                return await session.call_tool(tool_name, arguments=arguments)

            result = await self._with_session(_call)

            # Extract text blocks based on MCP protocol
            content_blocks = getattr(result, "content", []) or []
            text_result = " ".join([block.text for block in content_blocks if hasattr(block, "text")])

            # Format as a standard JSON string for the LLM
            try:
                parsed = json.loads(text_result)
                return json.dumps({"status": "success", "data": parsed})
            except json.JSONDecodeError:
                return json.dumps({"status": "success", "data": text_result})

        except Exception as e:
            LOGGER.error(f"Error executing Grafana tool {tool_name}: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    async def get_tools(self, llm_model_name: str = "qwen2.5-coder") -> list[StructuredTool]:
        """Fetches available tools from MCP and converts them to LangChain format."""
        if self._tools_cache is not None:
            return self._tools_cache

        async def _list(session: ClientSession):
            return await session.list_tools()

        mcp_tools = await self._with_session(_list)

        # Context Optimization Logic (Preserved from your original codebase)
        is_small_model = any(
            keyword in llm_model_name.lower()
            for keyword in ["gemma", "3b", "7b", "8b", "qwen2.5"]
        )
        should_filter = (self.optimize_context == "true") or (self.optimize_context == "auto" and is_small_model)

        def _make_tool_wrapper(tool_name: str) -> Callable[..., Awaitable[str]]:
            async def tool_wrapper(**kwargs) -> str:
                return await self.execute_tool(tool_name, kwargs)

            tool_wrapper.__name__ = tool_name
            return tool_wrapper

        langchain_tools = []
        for tool in mcp_tools.tools:
            if should_filter and tool.name not in self.allowed_tools:
                continue

            bound_wrapper = _make_tool_wrapper(tool.name)
            bound_wrapper.__doc__ = tool.description or f"Execute MCP tool {tool.name}."

            lc_tool = StructuredTool.from_function(
                coroutine=bound_wrapper,
                name=tool.name,
                description=tool.description,
            )
            langchain_tools.append(lc_tool)

        self._tools_cache = langchain_tools
        LOGGER.info(f"Loaded {len(langchain_tools)} Grafana MCP tools.")
        return langchain_tools

    async def close(self):
        """Reset local cache. No long-lived session is kept."""
        self._tools_cache = None