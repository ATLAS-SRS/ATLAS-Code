from dataclasses import dataclass
from typing import Any, Awaitable, Callable

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
