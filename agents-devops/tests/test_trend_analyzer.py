"""Tests for the refactored trend analyzer (v2) with Pydantic models."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Mock the database module before importing trend_analyzer_v2
sys.modules["src.database"] = MagicMock()
sys.modules["src.agent_guardian.config"] = MagicMock()

# Import directly from the source file to test Pydantic models
import importlib.util

spec = importlib.util.spec_from_file_location(
    "trend_analyzer_v2",
    Path(__file__).parent.parent / "src" / "agent_trend" / "trend_analyzer.py",
)
trend_analyzer_v2 = importlib.util.module_from_spec(spec)
sys.modules["trend_analyzer_v2"] = trend_analyzer_v2

# Mock dependencies before loading
sys.modules["src.database"].fetch_recent_incident_reports = MagicMock()
sys.modules["src.database"].fetch_recent_incident_reports_sync = MagicMock()
sys.modules["src.database"].get_async_session = MagicMock()
sys.modules["src.database"].get_sync_session = MagicMock()
sys.modules["src.database"].upsert_trend_report = MagicMock()
sys.modules["src.database"].upsert_trend_report_sync = MagicMock()

sys.modules["src.agent_guardian.config"].LOGGER = MagicMock()
sys.modules["src.agent_guardian.config"].REQUEST_TIMEOUT_SECONDS = 30

try:
    sys.modules["src.agent_guardian.utils"]._clean_json_markdown = lambda x: x.strip()
except Exception:
    pass

spec.loader.exec_module(trend_analyzer_v2)

IncidentSummary = trend_analyzer_v2.IncidentSummary
LLMResponse = trend_analyzer_v2.LLMResponse
ReplicaStatus = trend_analyzer_v2.ReplicaStatus
TrendReport = trend_analyzer_v2.TrendReport
_normalize_incident = trend_analyzer_v2._normalize_incident
_occurrence_count = trend_analyzer_v2._occurrence_count
_parse_llm_response = trend_analyzer_v2._parse_llm_response
_trend_id = trend_analyzer_v2._trend_id
_extract_replica_summary = trend_analyzer_v2._extract_replica_summary


class TestPydanticModels:
    """Test Pydantic model validation and serialization."""

    def test_replica_status_valid(self):
        """Valid replica status parses correctly."""
        replica = ReplicaStatus(current_replicas=3, desired_replicas=5)
        assert replica.current_replicas == 3
        assert replica.desired_replicas == 5

    def test_replica_status_extra_fields_ignored(self):
        """Extra fields in ReplicaStatus are ignored."""
        replica = ReplicaStatus(current_replicas=2, unknown_field="ignored")
        assert replica.current_replicas == 2

    def test_incident_summary_valid(self):
        """IncidentSummary validates required and optional fields."""
        incident = IncidentSummary(
            incident_id="inc-123",
            time="2026-05-07T10:00:00Z",
            verdict="CRITICAL",
        )
        assert incident.incident_id == "inc-123"
        assert incident.verdict == "CRITICAL"
        assert incident.action_taken == "UNKNOWN"  # default

    def test_llm_response_coerces_strings(self):
        """LLMResponse coerces non-string values to strings."""
        response = LLMResponse(
            pattern_detected=123,  # Should coerce to "123"
            root_cause_hypothesis=None,  # Should coerce to ""
            long_term_recommendation="test",
        )
        assert isinstance(response.pattern_detected, str)
        assert response.pattern_detected == "123"
        assert response.root_cause_hypothesis == ""

    def test_llm_response_serialization(self):
        """LLMResponse serializes to dict correctly."""
        response = LLMResponse(
            pattern_detected="Pattern found",
            root_cause_hypothesis="Memory leak",
            long_term_recommendation="Upgrade instances",
        )
        d = response.model_dump()
        assert d["pattern_detected"] == "Pattern found"
        assert d["root_cause_hypothesis"] == "Memory leak"

    def test_trend_report_to_dict(self):
        """TrendReport.to_dict() serializes for database storage."""
        report = TrendReport(
            trend_id="trend-test-001",
            generated_at_utc="2026-05-07T10:00:00Z",
            deployment="api-gateway",
            analysis_window_hours=24,
            incident_count=2,
            total_occurrences=3,
            source_incident_ids=["inc-1", "inc-2"],
            incident_summaries=[
                IncidentSummary(incident_id="inc-1", time="2026-05-07T09:00:00Z"),
                IncidentSummary(incident_id="inc-2", time="2026-05-07T10:00:00Z"),
            ],
            pattern_detected="High latency",
            root_cause_hypothesis="Connection pool exhaustion",
            long_term_recommendation="Increase pool size",
        )
        d = report.to_dict()
        assert d["trend_id"] == "trend-test-001"
        assert d["deployment"] == "api-gateway"
        assert d["incident_count"] == 2
        assert len(d["incident_summaries"]) == 2


class TestHelperFunctions:
    """Test utility functions."""

    def test_occurrence_count_valid_integer(self):
        """occurrence_count extracts integer correctly."""
        incident = {"occurrence_count": 5}
        assert _occurrence_count(incident) == 5

    def test_occurrence_count_defaults_to_one(self):
        """occurrence_count defaults to 1 if missing."""
        incident = {}
        assert _occurrence_count(incident) == 1

    def test_occurrence_count_coerces_string(self):
        """occurrence_count coerces string to int."""
        incident = {"occurrence_count": "10"}
        assert _occurrence_count(incident) == 10

    def test_occurrence_count_invalid_defaults_to_one(self):
        """occurrence_count defaults to 1 on ValueError."""
        incident = {"occurrence_count": "invalid"}
        assert _occurrence_count(incident) == 1

    def test_extract_replica_summary_from_dict(self):
        """extract_replica_summary extracts replica info."""
        incident = {
            "execution_details": {
                "replicas": {
                    "current_replicas": 3,
                    "desired_replicas": 5,
                    "extra": "ignored",
                }
            }
        }
        replica = _extract_replica_summary(incident)
        assert replica is not None
        assert replica.current_replicas == 3
        assert replica.desired_replicas == 5

    def test_extract_replica_summary_missing_returns_none(self):
        """extract_replica_summary returns None if missing."""
        incident = {"execution_details": {}}
        replica = _extract_replica_summary(incident)
        assert replica is None

    def test_extract_replica_summary_not_dict_returns_none(self):
        """extract_replica_summary returns None if replicas not a dict."""
        incident = {"execution_details": {"replicas": "not-a-dict"}}
        replica = _extract_replica_summary(incident)
        assert replica is None

    def test_normalize_incident_full(self):
        """normalize_incident converts raw dict to IncidentSummary."""
        incident = {
            "incident_id": "inc-123",
            "last_seen_utc": "2026-05-07T10:00:00Z",
            "investigation": {"verdict": "CRITICAL"},
            "execution_details": {
                "action": "scale_up",
                "outcome": "success",
                "follow_up": "monitor",
                "replicas": {"current_replicas": 5},
            },
            "alert_context": {"summary": "High load"},
        }
        summary = _normalize_incident(incident)
        assert summary.incident_id == "inc-123"
        assert summary.verdict == "CRITICAL"
        assert summary.action_taken == "scale_up"
        assert summary.outcome == "success"
        assert summary.summary == "High load"

    def test_normalize_incident_minimal(self):
        """normalize_incident handles minimal incident data."""
        incident = {"incident_id": "inc-456"}
        summary = _normalize_incident(incident)
        assert summary.incident_id == "inc-456"
        assert summary.verdict == "UNKNOWN"
        assert summary.action_taken == "UNKNOWN"

    def test_trend_id_deterministic(self):
        """trend_id produces same output for same inputs."""
        dt = datetime(2026, 5, 7, 10, 0, 0, tzinfo=timezone.utc)
        id1 = _trend_id("api-gateway", dt)
        id2 = _trend_id("api-gateway", dt)
        assert id1 == id2
        assert id1.startswith("trend-api-gateway-")

    def test_trend_id_changes_with_timestamp(self):
        """trend_id changes when timestamp changes."""
        dt1 = datetime(2026, 5, 7, 10, 0, 0, tzinfo=timezone.utc)
        dt2 = datetime(2026, 5, 7, 11, 0, 0, tzinfo=timezone.utc)
        id1 = _trend_id("api-gateway", dt1)
        id2 = _trend_id("api-gateway", dt2)
        assert id1 != id2

    def test_parse_llm_response_valid_json(self):
        """parse_llm_response parses valid JSON."""
        json_str = json.dumps({
            "pattern_detected": "Memory leak",
            "root_cause_hypothesis": "Unbounded cache",
            "long_term_recommendation": "Add cache limits",
        })
        response = _parse_llm_response(json_str, "api-gateway")
        assert response.pattern_detected == "Memory leak"
        assert response.root_cause_hypothesis == "Unbounded cache"

    def test_parse_llm_response_markdown_wrapped(self):
        """parse_llm_response handles markdown-wrapped JSON."""
        markdown_json = """```json
{
    "pattern_detected": "High latency",
    "root_cause_hypothesis": "CPU throttling",
    "long_term_recommendation": "Scale horizontally"
}
```"""
        response = _parse_llm_response(markdown_json, "api-gateway")
        assert response.pattern_detected == "High latency"

    def test_parse_llm_response_invalid_json_fallback(self):
        """parse_llm_response falls back gracefully on invalid JSON."""
        response = _parse_llm_response("not valid json at all", "api-gateway")
        assert response.pattern_detected == "Unable to parse trend response"
        assert "not valid json" in response.root_cause_hypothesis

    def test_parse_llm_response_missing_field_uses_fallback(self):
        """parse_llm_response falls back when required fields are missing."""
        json_str = json.dumps({
            "pattern_detected": "Found pattern",
            # missing other required fields
        })
        response = _parse_llm_response(json_str, "api-gateway")
        # Pydantic validation fails, so fallback is used
        assert response.pattern_detected == "Unexpected response format"
        assert "Found pattern" in response.root_cause_hypothesis or len(response.root_cause_hypothesis) > 0


class TestValidationEdgeCases:
    """Test edge cases and error handling."""

    def test_incident_summary_with_replica_object(self):
        """IncidentSummary accepts ReplicaStatus object."""
        replica = ReplicaStatus(current_replicas=2)
        summary = IncidentSummary(
            incident_id="inc-1",
            time="2026-05-07T10:00:00Z",
            replicas=replica,
        )
        assert summary.replicas.current_replicas == 2

    def test_trend_report_empty_incident_summaries(self):
        """TrendReport allows empty incident_summaries."""
        report = TrendReport(
            trend_id="trend-001",
            generated_at_utc="2026-05-07T10:00:00Z",
            deployment="test",
            analysis_window_hours=24,
            incident_count=0,
            total_occurrences=0,
            source_incident_ids=[],
            incident_summaries=[],
            pattern_detected="None",
            root_cause_hypothesis="No incidents",
            long_term_recommendation="Monitor",
        )
        assert len(report.incident_summaries) == 0

    def test_llm_response_numeric_values_coerced(self):
        """LLMResponse coerces numeric values to strings."""
        response = LLMResponse(
            pattern_detected=404,
            root_cause_hypothesis=0.5,
            long_term_recommendation=True,
        )
        assert response.pattern_detected == "404"
        assert response.root_cause_hypothesis == "0.5"
        assert response.long_term_recommendation == "True"
