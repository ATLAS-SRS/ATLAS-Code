import asyncio
from contextlib import asynccontextmanager

from src.agent_guardian.runtime import SREGuardianRuntime


def test_reporting_upserts_same_fingerprint(monkeypatch) -> None:
    stored_reports: dict[str, dict] = {}

    @asynccontextmanager
    async def fake_session():
        yield object()

    async def fake_fetch_incident_report(_session, incident_id: str):
        return stored_reports.get(incident_id)

    async def fake_upsert_incident_report(_session, *, incident_id: str, timestamp_utc, deployment: str, report_data: dict):
        del timestamp_utc, deployment
        stored_reports[incident_id] = report_data

    monkeypatch.setattr("src.agent_guardian.runtime.get_async_session", fake_session)
    monkeypatch.setattr("src.agent_guardian.runtime.fetch_incident_report", fake_fetch_incident_report)
    monkeypatch.setattr("src.agent_guardian.runtime.upsert_incident_report", fake_upsert_incident_report)

    runtime = SREGuardianRuntime()
    runtime._llm_client = object()
    runtime._graph = runtime._build_graph()

    alert = {
        "status": "firing",
        "fingerprint": "incident-123",
        "startsAt": "2026-05-06T10:00:00Z",
        "labels": {
            "alertname": "HighLatency",
            "severity": "critical",
            "service": "api-gateway",
        },
        "annotations": {
            "summary": "Latency spike",
        },
    }

    first_state = asyncio.run(runtime.graph.ainvoke({"alert": alert}))
    second_state = asyncio.run(runtime.graph.ainvoke({"alert": alert}))

    assert first_state["incident_id"] == "incident-123"
    assert second_state["incident_id"] == "incident-123"

    report = stored_reports["incident-123"]
    assert report["occurrence_count"] == 2
    assert report["first_seen_utc"] <= report["last_seen_utc"]
    assert report["incident_id"] == "incident-123"


def test_reporting_uses_stable_fallback_incident_key(monkeypatch) -> None:
    stored_reports: dict[str, dict] = {}

    @asynccontextmanager
    async def fake_session():
        yield object()

    async def fake_fetch_incident_report(_session, incident_id: str):
        return stored_reports.get(incident_id)

    async def fake_upsert_incident_report(_session, *, incident_id: str, timestamp_utc, deployment: str, report_data: dict):
        del timestamp_utc, deployment
        stored_reports[incident_id] = report_data

    monkeypatch.setattr("src.agent_guardian.runtime.get_async_session", fake_session)
    monkeypatch.setattr("src.agent_guardian.runtime.fetch_incident_report", fake_fetch_incident_report)
    monkeypatch.setattr("src.agent_guardian.runtime.upsert_incident_report", fake_upsert_incident_report)

    runtime = SREGuardianRuntime()
    runtime._llm_client = object()
    runtime._graph = runtime._build_graph()

    alert = {
        "status": "firing",
        "startsAt": "2026-05-06T10:00:00Z",
        "labels": {
            "alertname": "HighLatency",
            "severity": "critical",
            "service": "api-gateway",
            "namespace": "default",
        },
        "annotations": {
            "summary": "Latency spike",
        },
    }

    first_state = asyncio.run(runtime.graph.ainvoke({"alert": alert}))
    second_state = asyncio.run(runtime.graph.ainvoke({"alert": alert}))

    assert first_state["incident_id"] == second_state["incident_id"]
    assert len(stored_reports) == 1

    report = next(iter(stored_reports.values()))
    assert report["occurrence_count"] == 2
