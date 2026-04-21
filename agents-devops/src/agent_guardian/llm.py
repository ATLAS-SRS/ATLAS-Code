import json
from typing import Any
from openai import AsyncOpenAI

from .config import REQUEST_TIMEOUT_SECONDS, LOGGER
from .registry import ToolRegistry
from .utils import _tool_call_parts, _safe_json, _clean_json_markdown

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

        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": 0.0,
            "timeout": REQUEST_TIMEOUT_SECONDS,
        }
        if schemas:
            kwargs["tools"] = schemas
            kwargs["tool_choice"] = "auto"

        response = await llm_client.chat.completions.create(**kwargs)

        assistant_msg = response.choices[0].message
        msg_dict = assistant_msg.model_dump(exclude_unset=True)
        messages.append(msg_dict)

        tool_calls = msg_dict.get("tool_calls") or []
        if not tool_calls:
            return (_clean_json_markdown(assistant_msg.content or ""), messages)

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
