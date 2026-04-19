from typing import Any

from structured_logger import get_logger

from .state import MultiAgentState


LOGGER = get_logger("guardrail-router")

MAX_LOOP_COUNT = 3
CONFIDENCE_THRESHOLD = 0.80
MOCK_COST_PER_REPLICA = 2.5
MAX_MOCK_HOURLY_COST = 50.0


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_action(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def guardrail_router(state: MultiAgentState) -> str:
    loop_count = _coerce_int(state.get("loop_count", 0))
    confidence = _coerce_float(state.get("confidence", 0.0))
    proposed_action = _normalize_action(state.get("proposed_action"))
    target_replicas = _coerce_int(state.get("target_replicas", 0))

    if loop_count >= MAX_LOOP_COUNT:
        return "HumanApproval"

    if confidence < CONFIDENCE_THRESHOLD:
        return "HumanApproval"

    if proposed_action == "scale":
        if target_replicas <= 0:
            return "HumanApproval"

        estimated_hourly_cost = target_replicas * MOCK_COST_PER_REPLICA
        if estimated_hourly_cost > MAX_MOCK_HOURLY_COST:
            return "HumanApproval"

        return "ActionNode"

    return "InvestigateNode"


def human_approval_node(state: MultiAgentState) -> dict[str, Any]:
    LOGGER.info(
        "Human approval required",
        extra={
            "deployment": state.get("deployment", ""),
            "loop_count": state.get("loop_count", 0),
            "proposed_action": state.get("proposed_action", ""),
        },
    )
    return {"approval_status": "waiting_for_human"}