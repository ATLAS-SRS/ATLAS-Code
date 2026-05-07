from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AlertContextModel(BaseModel):
    alert_name: str
    severity: str
    status: str
    deployment: str
    workload_policy: str
    summary: str = ""
    description: str = ""
    startsAt: str = ""
    endsAt: str = ""
    fingerprint: str = ""
    generatorURL: str = ""
    labels: dict[str, Any] = Field(default_factory=dict)


class InvestigationReportModel(BaseModel):
    verdict: Literal["TRAFFIC", "BUG", "INFRASTRUCTURE", "UNCLEAR", "UNKNOWN"] = "UNKNOWN"
    confidence: float | None = None
    evidence: list[str] = Field(default_factory=list)
    recommended_next_step: str = ""


class ActionDecisionModel(BaseModel):
    action: Literal[
        "SCALE_UP",
        "HOLD",
        "ROLLBACK_RECOMMENDATION",
        "RESTORE_LIMITS",
        "UNKNOWN",
    ] = "UNKNOWN"
    deployment: str = ""
    rationale: str = ""
    executed_tools: list[str] = Field(default_factory=list)
    outcome: str = ""
    follow_up: str = ""
    detailed_incident_report: str = ""
    human_approval: str = "not_required"
    approval: str | None = None


class ExecutionDetailsModel(BaseModel):
    action: ActionDecisionModel
    deployment: str
    replicas: dict[str, Any] = Field(default_factory=dict)
    human_approval: str = "not_required"
    rationale: str = ""
    executed_tools: list[str] = Field(default_factory=list)
    outcome: str = ""
    follow_up: str = ""


class IncidentReportModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    incident_id: str
    first_seen_utc: str
    last_seen_utc: str
    timestamp_utc: str
    occurrence_count: int
    current_status: str
    alert_context: AlertContextModel
    investigation: InvestigationReportModel
    execution_details: ExecutionDetailsModel
    final_summary: str
    error: str = ""