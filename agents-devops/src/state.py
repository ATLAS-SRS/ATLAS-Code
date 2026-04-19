from typing import Any, TypedDict


class MultiAgentState(TypedDict, total=False):
    deployment: str
    messages: list[dict[str, Any]]
    step_count: int
    final_report: str
    last_error: str
    loop_count: int
    proposed_action: str
    target_replicas: int
    confidence: float
    approval_status: str | None


# Backward-compatible alias for the current runtime naming.
AgentState = MultiAgentState


def build_initial_state(
    deployment: str,
    messages: list[dict[str, Any]] | None = None,
) -> MultiAgentState:
    return {
        "deployment": deployment,
        "messages": list(messages or []),
        "step_count": 0,
        "final_report": "",
        "last_error": "",
        "loop_count": 0,
        "proposed_action": "",
        "target_replicas": 0,
        "confidence": 0.0,
        "approval_status": None,
    }