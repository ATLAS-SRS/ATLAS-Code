import os
import json
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from langchain_core.tools import StructuredTool
from structured_logger import get_logger

LOGGER = get_logger("grafana-mcp-tools")

class GrafanaMCPManager:
    """Manages the SSE connection to Grafana MCP and provides LangChain tools."""
    
    def __init__(self):
        self.mcp_url = os.getenv(
            "GRAFANA_MCP_URL",
            "http://grafana-mcp-server.default.svc.cluster.local:80/sse"
        ).strip()
        self.optimize_context = os.getenv("OPTIMIZE_CONTEXT", "auto").lower()
        self.allowed_tools = {"query_prometheus", "query_loki_logs"}
        
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None
        self._tools_cache: list[StructuredTool] | None = None

    async def connect(self) -> ClientSession:
        """Establishes or returns the existing SSE connection to Grafana MCP."""
        if self._session:
            return self._session

        LOGGER.info(f"Connecting to Grafana MCP at {self.mcp_url}")
        try:
            transport = await self._stack.enter_async_context(sse_client(self.mcp_url))
            self._session = await self._stack.enter_async_context(ClientSession(*transport))
            await self._session.initialize()
            LOGGER.info("Grafana MCP connected successfully.")
            return self._session
        except Exception as e:
            LOGGER.error(f"Failed to connect to Grafana MCP: {e}")
            raise

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Executes a tool on the remote MCP server and formats the output."""
        session = await self.connect()
        try:
            result = await session.call_tool(tool_name, arguments=arguments)
            
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

        session = await self.connect()
        mcp_tools = await session.list_tools()
        
        # Context Optimization Logic (Preserved from your original codebase)
        is_small_model = any(
            keyword in llm_model_name.lower() 
            for keyword in ["gemma", "3b", "7b", "8b", "qwen2.5"]
        )
        should_filter = (self.optimize_context == "true") or (self.optimize_context == "auto" and is_small_model)
        
        langchain_tools = []
        for tool in mcp_tools.tools:
            if should_filter and tool.name not in self.allowed_tools:
                continue
                
            # Create an async wrapper function dynamically bound to this specific tool's name
            async def tool_wrapper(**kwargs) -> str:
                current_tool_name = kwargs.pop("__mcp_tool_name") 
                return await self.execute_tool(current_tool_name, kwargs)

            import functools
            bound_wrapper = functools.partial(tool_wrapper, __mcp_tool_name=tool.name)
            bound_wrapper.__name__ = tool.name
            bound_wrapper.__doc__ = tool.description

            lc_tool = StructuredTool.from_function(
                coroutine=bound_wrapper,
                name=tool.name,
                description=tool.description,
                # LangChain cercherà di dedurre gli argomenti, ma se non ci riesce
                # il modo migliore (più avanzato) sarebbe convertire tool.inputSchema 
                # in un modello Pydantic. Per ora testalo così, spesso LangChain 
                # riesce a dedurlo dalla descrizione se è ben scritta.
            )
            langchain_tools.append(lc_tool)
            
            # Explicitly set metadata so LangChain can pass it to the LLM
            tool_wrapper.__name__ = tool.name
            tool_wrapper.__doc__ = tool.description

            lc_tool = StructuredTool.from_function(
                coroutine=tool_wrapper,
                name=tool.name,
                description=tool.description,
            )
            langchain_tools.append(lc_tool)
            
        self._tools_cache = langchain_tools
        LOGGER.info(f"Loaded {len(langchain_tools)} Grafana MCP tools.")
        return langchain_tools

    async def close(self):
        """Gracefully closes the MCP connection. Call this when shutting down FastAPI."""
        await self._stack.aclose()
        self._session = None
        self._tools_cache = None