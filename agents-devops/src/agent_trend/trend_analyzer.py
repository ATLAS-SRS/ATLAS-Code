import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from openai import AsyncOpenAI, OpenAI
from pydantic import BaseModel, Field, field_validator

from src.database import (
    fetch_recent_incident_reports,
    fetch_recent_incident_reports_sync,
    get_async_session,
    get_sync_session,
    upsert_trend_report,
    upsert_trend_report_sync,
)
from src.agent_guardian.config import LOGGER, REQUEST_TIMEOUT_SECONDS

try:
    from src.agent_guardian.utils import _clean_json_markdown
except ImportError as e:
    LOGGER.warning(f"Failed to import _clean_json_markdown from utils, using fallback: {e}")

    def _clean_json_markdown(text: str) -> str:
        return text.strip()


# Configuration
TREND_INCIDENT_THRESHOLD = 1
ANALYSIS_WINDOW_HOURS = 24


# ============================================================================
# Pydantic Models for Type Safety & Validation
# ============================================================================


class ReplicaStatus(BaseModel):
    """Kubernetes replica state from execution details."""

    current_replicas: int | None = None
    desired_replicas: int | None = None
    target_replicas: int | None = None
    replicas: int | None = None

    model_config = {"extra": "ignore"}


class IncidentSummary(BaseModel):
    """Normalized incident summary for trend analysis."""

    incident_id: str
    time: str
    verdict: str = "UNKNOWN"
    action_taken: str = "UNKNOWN"
    replicas: ReplicaStatus | None = None
    outcome: str = ""
    follow_up: str = ""
    summary: str = ""

    model_config = {"extra": "ignore"}


class LLMResponse(BaseModel):
    """Validated LLM response for trend analysis."""

    pattern_detected: str
    root_cause_hypothesis: str
    long_term_recommendation: str

    model_config = {"extra": "ignore"}

    @field_validator("pattern_detected", "root_cause_hypothesis", "long_term_recommendation", mode="before")
    @classmethod
    def ensure_string(cls, v: Any) -> str:
        """Coerce any type to string safely."""
        if isinstance(v, str):
            return v
        return str(v) if v is not None else ""


class TrendReport(BaseModel):
    """Complete trend analysis report."""

    trend_id: str
    generated_at_utc: str
    deployment: str
    analysis_window_hours: int
    incident_count: int
    total_occurrences: int
    source_incident_ids: list[str]
    incident_summaries: list[IncidentSummary]
    pattern_detected: str
    root_cause_hypothesis: str
    long_term_recommendation: str

    model_config = {"extra": "ignore"}

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for database storage."""
        return self.model_dump(mode="json", exclude_none=True)


# ============================================================================
# Helper Functions
# ============================================================================


def _extract_replica_summary(incident: dict[str, Any]) -> ReplicaStatus | None:
    """Extract and validate replica information from incident."""
    replicas = (incident.get("execution_details") or {}).get("replicas")
    if isinstance(replicas, dict):
        try:
            return ReplicaStatus(**replicas)
        except Exception:
            return None
    return None


def _occurrence_count(incident: dict[str, Any]) -> int:
    """Extract occurrence count with safe fallback."""
    try:
        count = int(incident.get("occurrence_count", 1) or 1)
        return max(1, count)
    except (TypeError, ValueError):
        return 1


def _trend_id(deployment: str, generated_at_utc: datetime) -> str:
    """Generate deterministic trend ID."""
    timestamp = generated_at_utc.strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(f"{deployment}:{timestamp}".encode("utf-8")).hexdigest()[:10]
    return f"trend-{deployment or 'unknown'}-{timestamp}-{digest}"


def _normalize_incident(incident: dict[str, Any]) -> IncidentSummary:
    """Convert raw incident dict to validated IncidentSummary."""
    return IncidentSummary(
        incident_id=incident.get("incident_id", ""),
        time=incident.get("last_seen_utc") or incident.get("timestamp_utc", ""),
        verdict=(incident.get("investigation") or {}).get("verdict", "UNKNOWN"),
        action_taken=(incident.get("execution_details") or {}).get("action", "UNKNOWN"),
        replicas=_extract_replica_summary(incident),
        outcome=(incident.get("execution_details") or {}).get("outcome", ""),
        follow_up=(incident.get("execution_details") or {}).get("follow_up", ""),
        summary=(incident.get("alert_context") or {}).get("summary", ""),
    )


def _build_llm_prompt(deployment: str, incidents: list[IncidentSummary], total_occurrences: int) -> list[dict[str, str]]:
    """Build LLM messages for trend analysis."""
    return [
        {
            "role": "system",
            "content": (
                "You are a senior SRE problem management analyst. "
                "Review repeated incident summaries for one Kubernetes deployment. "
                "Identify chronic patterns, judge whether the reactive actions look like temporary band-aids, "
                "and recommend durable infrastructure or code fixes. "
                "Return only a JSON object with keys: pattern_detected, root_cause_hypothesis, long_term_recommendation."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "deployment": deployment,
                    "analysis_window_hours": ANALYSIS_WINDOW_HOURS,
                    "incident_count": len(incidents),
                    "total_occurrences": total_occurrences,
                    "incidents": [incident.model_dump() for incident in incidents],
                },
                ensure_ascii=True,
                indent=2,
            ),
        },
    ]


def _parse_llm_response(raw_content: str, deployment: str) -> LLMResponse:
    """Parse and validate LLM response with fallbacks."""
    cleaned = _clean_json_markdown(raw_content)
    try:
        payload = json.loads(cleaned)
        return LLMResponse(**payload)
    except json.JSONDecodeError as e:
        LOGGER.warning(
            f"LLM response not valid JSON: {e}",
            extra={"deployment": deployment, "preview": cleaned[:200]},
        )
        return LLMResponse(
            pattern_detected="Unable to parse trend response",
            root_cause_hypothesis=cleaned[:500],
            long_term_recommendation="Review model output; response was not valid JSON.",
        )
    except Exception as e:
        LOGGER.warning(
            f"LLM response validation failed: {e}",
            extra={"deployment": deployment},
        )
        return LLMResponse(
            pattern_detected="Unexpected response format",
            root_cause_hypothesis=str(cleaned)[:500],
            long_term_recommendation="Tighten the prompt or model settings.",
        )


async def _analyze_deployment(
    session: Any,
    llm_client: AsyncOpenAI,
    model: str,
    deployment: str,
    incidents: list[dict[str, Any]],
) -> str | None:
    """
    Analyze a single deployment's incidents and generate trend report (async).
    Returns trend_id on success, None on failure.
    """
    total_occurrences = sum(_occurrence_count(incident) for incident in incidents)
    if total_occurrences < TREND_INCIDENT_THRESHOLD:
        return None

    try:
        # Normalize and sort incidents
        normalized = [_normalize_incident(inc) for inc in incidents]
        normalized.sort(key=lambda x: x.time or "", reverse=True)

        # Query LLM
        messages = _build_llm_prompt(deployment, normalized, total_occurrences)
        response = await llm_client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.0,
            timeout=REQUEST_TIMEOUT_SECONDS,
            response_format={"type": "json_object"},
        )

        raw_content = response.choices[0].message.content or "{}"
        llm_response = _parse_llm_response(raw_content, deployment)

        # Build and save trend report
        generated_at_utc = datetime.now(timezone.utc)
        trend_id = _trend_id(deployment, generated_at_utc)

        trend_report = TrendReport(
            trend_id=trend_id,
            generated_at_utc=generated_at_utc.isoformat().replace("+00:00", "Z"),
            deployment=deployment,
            analysis_window_hours=ANALYSIS_WINDOW_HOURS,
            incident_count=len(normalized),
            total_occurrences=total_occurrences,
            source_incident_ids=[inc.incident_id for inc in normalized],
            incident_summaries=normalized,
            pattern_detected=llm_response.pattern_detected,
            root_cause_hypothesis=llm_response.root_cause_hypothesis,
            long_term_recommendation=llm_response.long_term_recommendation,
        )

        await upsert_trend_report(
            session,
            trend_id=trend_id,
            generated_at_utc=generated_at_utc,
            deployment=deployment,
            report_data=trend_report.to_dict(),
        )

        LOGGER.info(
            f"Trend report generated: {deployment}",
            extra={
                "trend_id": trend_id,
                "incident_count": len(normalized),
                "total_occurrences": total_occurrences,
            },
        )
        return trend_id

    except Exception as e:
        LOGGER.error(
            f"Trend analysis failed: {deployment}",
            extra={"error": str(e)},
        )
        return None


def _analyze_deployment_sync(
    session: Any,
    llm_client: OpenAI,
    model: str,
    deployment: str,
    incidents: list[dict[str, Any]],
) -> str | None:
    """
    Analyze a single deployment's incidents and generate trend report (sync).
    Returns trend_id on success, None on failure.
    """
    total_occurrences = sum(_occurrence_count(incident) for incident in incidents)
    if total_occurrences < TREND_INCIDENT_THRESHOLD:
        return None

    try:
        # Normalize and sort incidents
        normalized = [_normalize_incident(inc) for inc in incidents]
        normalized.sort(key=lambda x: x.time or "", reverse=True)

        # Query LLM
        messages = _build_llm_prompt(deployment, normalized, total_occurrences)
        response = llm_client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.0,
            timeout=REQUEST_TIMEOUT_SECONDS,
            response_format={"type": "json_object"},
        )

        raw_content = response.choices[0].message.content or "{}"
        llm_response = _parse_llm_response(raw_content, deployment)

        # Build and save trend report
        generated_at_utc = datetime.now(timezone.utc)
        trend_id = _trend_id(deployment, generated_at_utc)

        trend_report = TrendReport(
            trend_id=trend_id,
            generated_at_utc=generated_at_utc.isoformat().replace("+00:00", "Z"),
            deployment=deployment,
            analysis_window_hours=ANALYSIS_WINDOW_HOURS,
            incident_count=len(normalized),
            total_occurrences=total_occurrences,
            source_incident_ids=[inc.incident_id for inc in normalized],
            incident_summaries=normalized,
            pattern_detected=llm_response.pattern_detected,
            root_cause_hypothesis=llm_response.root_cause_hypothesis,
            long_term_recommendation=llm_response.long_term_recommendation,
        )

        upsert_trend_report_sync(
            session,
            trend_id=trend_id,
            generated_at_utc=generated_at_utc,
            deployment=deployment,
            report_data=trend_report.to_dict(),
        )

        LOGGER.info(
            f"Trend report generated: {deployment}",
            extra={
                "trend_id": trend_id,
                "incident_count": len(normalized),
                "total_occurrences": total_occurrences,
            },
        )
        return trend_id

    except Exception as e:
        LOGGER.error(
            f"Trend analysis failed: {deployment}",
            extra={"error": str(e)},
        )
        return None


# ============================================================================
# Public API
# ============================================================================


async def analyze_recent_trends(
    llm_client: AsyncOpenAI | None,
    model: str,
    base_reports_dir: str | None = None,
) -> list[str]:
    """Analyze recent incidents and generate trend reports (async)."""
    del base_reports_dir

    if llm_client is None:
        LOGGER.warning("Trend analyzer skipped: LLM client not initialized")
        return []

    cutoff_utc = datetime.now(timezone.utc) - timedelta(hours=ANALYSIS_WINDOW_HOURS)
    async with get_async_session() as session:
        recent_incidents = await fetch_recent_incident_reports(session, cutoff_utc)

        if not recent_incidents:
            LOGGER.info(f"No incidents in last {ANALYSIS_WINDOW_HOURS} hours")
            return []

        # Group by deployment
        grouped_by_deployment: dict[str, list[dict[str, Any]]] = {}
        for incident in recent_incidents:
            deployment = ((incident.get("alert_context") or {}).get("deployment") or "").strip() or "unknown"
            grouped_by_deployment.setdefault(deployment, []).append(incident)

        # Analyze each deployment
        saved_reports: list[str] = []
        for deployment, incidents in grouped_by_deployment.items():
            trend_id = await _analyze_deployment(session, llm_client, model, deployment, incidents)
            if trend_id:
                saved_reports.append(trend_id)

        if not saved_reports:
            LOGGER.info("No deployments crossed incident threshold")

        return saved_reports


def analyze_recent_trends_sync(
    llm_client: OpenAI | None,
    model: str,
    base_reports_dir: str | None = None,
) -> list[str]:
    """Analyze recent incidents and generate trend reports (sync)."""
    del base_reports_dir

    if llm_client is None:
        LOGGER.warning("Trend analyzer skipped: LLM client not initialized")
        return []

    cutoff_utc = datetime.now(timezone.utc) - timedelta(hours=ANALYSIS_WINDOW_HOURS)
    with get_sync_session() as session:
        recent_incidents = fetch_recent_incident_reports_sync(session, cutoff_utc)

        if not recent_incidents:
            LOGGER.info(f"No incidents in last {ANALYSIS_WINDOW_HOURS} hours")
            return []

        # Group by deployment
        grouped_by_deployment: dict[str, list[dict[str, Any]]] = {}
        for incident in recent_incidents:
            deployment = ((incident.get("alert_context") or {}).get("deployment") or "").strip() or "unknown"
            grouped_by_deployment.setdefault(deployment, []).append(incident)

        # Analyze each deployment
        saved_reports: list[str] = []
        for deployment, incidents in grouped_by_deployment.items():
            trend_id = _analyze_deployment_sync(session, llm_client, model, deployment, incidents)
            if trend_id:
                saved_reports.append(trend_id)

        if not saved_reports:
            LOGGER.info("No deployments crossed incident threshold")

        return saved_reports
