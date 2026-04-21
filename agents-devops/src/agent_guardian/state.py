from typing import Any, TypedDict

class AgentState(TypedDict, total=False):
    alert: dict[str, Any]
    parsed_alert: dict[str, Any]
    investigation_report: str
    final_report: str
    llm_trace: list[dict[str, Any]]
    error: str
