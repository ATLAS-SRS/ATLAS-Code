from typing import Any, TypedDict

class AgentState(TypedDict, total=False):
    alert: dict[str, Any]
    parsed_alert: dict[str, Any]
    investigation_report: str
    final_report: str
    incident_id: str
    post_mortem_report: dict[str, Any]
    post_mortem_path: str
    llm_trace: list[dict[str, Any]]
    error: str
