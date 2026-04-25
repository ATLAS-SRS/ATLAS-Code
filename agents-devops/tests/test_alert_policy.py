from src.agent_guardian.utils import _extract_alert_fields


def test_extract_alert_fields_maps_database_pods_to_monitor_only() -> None:
    alert = {
        "labels": {
            "alertname": "PostgresDown",
            "pod": "atlas-postgres-postgresql-0",
        },
        "annotations": {
            "summary": "PostgreSQL pod is unavailable",
        },
        "status": "firing",
    }

    parsed = _extract_alert_fields(alert)

    assert parsed["deployment"] == "atlas-postgres-postgresql"
    assert parsed["workload_policy"] == "MONITOR_ONLY"


def test_extract_alert_fields_keeps_app_workloads_autoscalable() -> None:
    alert = {
        "labels": {
            "alertname": "ApiGatewayHighCpu",
            "pod": "api-gateway-7c9fcf9cdb-b7f2m",
        },
        "status": "firing",
    }

    parsed = _extract_alert_fields(alert)

    assert parsed["deployment"] == "api-gateway"
    assert parsed["workload_policy"] == "AUTOSCALE"
