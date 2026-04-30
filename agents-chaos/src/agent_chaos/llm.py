import json
from typing import Any

from openai import AsyncOpenAI

from .config import (
    LLM_API_KEY,
    LLM_API_URL,
    LLM_MODEL,
    REQUEST_TIMEOUT_SECONDS,
    _normalize_llm_base_url,
)


def create_litellm_client() -> AsyncOpenAI:
    """Create the chat client used by the Chaos agent reasoning node.

    The deployment uses an OpenAI-compatible endpoint (for example LiteLLM proxy or LM Studio).
    """
    return AsyncOpenAI(
        base_url=_normalize_llm_base_url(LLM_API_URL),
        api_key=LLM_API_KEY,
    )


async def call_litellm_with_tools(
    *,
    llm_client: AsyncOpenAI,
    messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
) -> dict[str, Any]:
    """Execute one model step with tool-binding enabled and normalize the response payload."""
    response = await llm_client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        tools=tool_schemas,
        tool_choice="auto",
        temperature=0.0,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    assistant = response.choices[0].message
    parsed_tool_calls: list[dict[str, Any]] = []
    for tc in assistant.tool_calls or []:
        raw_arguments = tc.function.arguments or "{}"
        try:
            parsed_arguments = json.loads(raw_arguments)
            if not isinstance(parsed_arguments, dict):
                parsed_arguments = {}
        except Exception:
            parsed_arguments = {}

        parsed_tool_calls.append(
            {
                "id": tc.id,
                "name": tc.function.name,
                "args": parsed_arguments,
            }
        )

    return {
        "content": assistant.content or "",
        "tool_calls": parsed_tool_calls,
    }
