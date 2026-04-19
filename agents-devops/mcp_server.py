#!/usr/bin/env python3
"""ATLAS MCP server for the all-in-one scaling agent runtime.

This process is spawned locally by mcp_client.py over stdio and exposes the
Prometheus/Kubernetes tools needed by the LLM loop.
"""

import os
import sys
from dataclasses import dataclass, field
from typing import Any

import requests
from kubernetes import client, config
from mcp.server.fastmcp import FastMCP
from structured_logger import get_logger


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOGGER = get_logger("mcp-server", stream=sys.stderr)
LOGGER.setLevel(LOG_LEVEL)

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090").strip()
NAMESPACE = os.getenv("NAMESPACE", "default").strip()
TARGET_DEPLOYMENTS = os.getenv(
    "TARGET_DEPLOYMENTS",
    "api-gateway,scoring-system,enrichment-system,notification-system",
)
PROMQL_RPS_QUERY = os.getenv(
    "PROMQL_RPS_QUERY",
    'sum(rate(http_requests_total{job="api-gateway"}[1m]))',
)
MIN_REPLICAS = int(os.getenv("MIN_REPLICAS", "1"))
MAX_REPLICAS = int(os.getenv("MAX_REPLICAS", "5"))
RPS_REPLICA_THRESHOLDS = os.getenv("RPS_REPLICA_THRESHOLDS", "5,15,30,60")

PROMQL_RPS_FALLBACK_QUERIES = [
    'sum(rate(http_requests_total{app_kubernetes_io_name="api-gateway"}[1m]))',
]


def log_event(level: str, message: str, **fields: object) -> None:
    log_fn = getattr(LOGGER, level.lower(), LOGGER.info)
    log_fn(message, extra=fields)


def _success_response(data: Any, message: str = "OK") -> dict[str, Any]:
    return {"status": "success", "message": message, "data": data}


def _error_response(message: str) -> dict[str, Any]:
    return {"status": "error", "message": message, "data": None}


def _load_kubernetes_config() -> tuple[bool, str | None]:
    try:
        config.load_incluster_config()
        log_event("info", "Loaded in-cluster Kubernetes config")
        return True, None
    except Exception as incluster_error:
        log_event(
            "warning",
            "Failed to load in-cluster Kubernetes config, trying local kube config",
            error=str(incluster_error),
        )

    try:
        config.load_kube_config()
        log_event("warning", "Loaded local kube config as fallback")
        return True, None
    except Exception as kubeconfig_error:
        error_message = (
            "Kubernetes config unavailable: failed both in-cluster and local kube config "
            f"load ({kubeconfig_error})"
        )
        log_event(
            "error",
            "Failed to load any Kubernetes configuration",
            incluster_error=str(incluster_error),
            kubeconfig_error=str(kubeconfig_error),
        )
        return False, error_message


def _run_prometheus_query(prometheus_base_url: str, query: str) -> list[dict[str, Any]]:
    endpoint = f"{prometheus_base_url.rstrip('/')}/api/v1/query"
    try:
        response = requests.get(endpoint, params={"query": query}, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get("status") != "success":
            log_event(
                "error",
                "Prometheus query returned non-success status",
                query=query,
                response=data,
            )
            return []

        result = data.get("data", {}).get("result", [])
        if not isinstance(result, list):
            log_event(
                "error",
                "Prometheus query returned invalid result format",
                query=query,
                result_type=type(result).__name__,
            )
            return []
        return result
    except requests.exceptions.RequestException as exc:
        log_event(
            "error",
            "Prometheus query failed due to request exception",
            query=query,
            endpoint=endpoint,
            error=str(exc),
        )
        return []
    except ValueError as exc:
        log_event(
            "error",
            "Prometheus query returned invalid JSON payload",
            query=query,
            endpoint=endpoint,
            error=str(exc),
        )
        return []


def _parse_rps_thresholds(raw_thresholds: str) -> list[float]:
    parts = [part.strip() for part in raw_thresholds.split(",") if part.strip()]
    if len(parts) != 4:
        raise RuntimeError(
            "RPS_REPLICA_THRESHOLDS must contain exactly 4 comma-separated numeric values"
        )

    thresholds = [float(value) for value in parts]
    if thresholds != sorted(thresholds):
        raise RuntimeError("RPS_REPLICA_THRESHOLDS must be sorted ascending")
    return thresholds


def _target_replicas_from_rps(rps: float, thresholds: list[float]) -> int:
    if rps <= thresholds[0]:
        return 1
    if rps <= thresholds[1]:
        return 2
    if rps <= thresholds[2]:
        return 3
    if rps <= thresholds[3]:
        return 4
    return 5


@dataclass
class AppState:
    apps_api: client.AppsV1Api | None
    namespace: str
    prometheus_url: str
    kubernetes_ready: bool
    kubernetes_error: str | None
    allowed_deployments: set[str] = field(default_factory=set)


STATE = AppState(
    apps_api=None,
    namespace=NAMESPACE,
    prometheus_url=PROMETHEUS_URL,
    kubernetes_ready=False,
    kubernetes_error="Runtime context is not initialized",
)
mcp = FastMCP("atlas-aiops-server")


@mcp.tool()
def get_scaling_recommendation(deployment: str) -> dict[str, Any]:
    """Return the current traffic-based scaling recommendation for a deployment."""

    log_event(
        "info",
        "Tool call started",
        tool="get_scaling_recommendation",
        deployment=deployment,
    )
    try:
        if deployment not in STATE.allowed_deployments:
            return _error_response(
                f"Deployment '{deployment}' non consentito. Consentiti: {sorted(STATE.allowed_deployments)}"
            )

        thresholds = _parse_rps_thresholds(RPS_REPLICA_THRESHOLDS)

        rps_result = get_rps()
        if rps_result.get("status") != "success":
            return _error_response(f"Unable to compute RPS: {rps_result.get('message')}")
        observed_rps = float(rps_result.get("data", 0.0))

        current_result = get_current_replicas(deployment=deployment)
        if current_result.get("status") != "success":
            return _error_response(
                f"Unable to read current replicas: {current_result.get('message')}"
            )
        current = int(current_result.get("data", 0))

        target = _target_replicas_from_rps(observed_rps, thresholds)
        target = max(MIN_REPLICAS, min(MAX_REPLICAS, target))

        if current < target:
            suggested_action = "SCALE_UP"
        elif current > target:
            suggested_action = "SCALE_DOWN"
        else:
            suggested_action = "HOLD"

        recommendation = {
            "deployment": deployment,
            "rps": round(observed_rps, 6),
            "current_replicas": current,
            "target_replicas": target,
            "suggested_action": suggested_action,
            "thresholds": thresholds,
            "reason": "Target calcolato da soglie RPS configurate",
        }
        log_event(
            "info",
            "Tool call completed",
            tool="get_scaling_recommendation",
            deployment=deployment,
            recommendation=recommendation,
        )
        return _success_response(recommendation)
    except Exception as exc:
        log_event(
            "error",
            "Tool call failed",
            tool="get_scaling_recommendation",
            deployment=deployment,
            error=str(exc),
        )
        return _error_response(str(exc))


@mcp.tool()
def get_rps() -> dict[str, Any]:
    """Read the global RPS from Prometheus, with a fallback query for live traffic."""

    queries = [PROMQL_RPS_QUERY, *PROMQL_RPS_FALLBACK_QUERIES]
    log_event("info", "Tool call started", tool="get_rps", queries=queries)
    try:
        attempts: list[dict[str, str]] = []

        for query in queries:
            result = _run_prometheus_query(STATE.prometheus_url, query)
            if not result:
                attempts.append({"query": query, "status": "empty_or_failed"})
                continue

            try:
                value = result[0].get("value", [None, "0"])[1]
                rps = float(value)
            except (IndexError, TypeError, ValueError) as exc:
                attempts.append({"query": query, "status": f"invalid_value:{exc}"})
                continue

            log_event(
                "info",
                "Tool call completed",
                tool="get_rps",
                queries=queries,
                selected_query=query,
                rps=round(rps, 6),
            )
            return _success_response(rps)

        log_event(
            "warning",
            "No RPS series found for configured queries; returning structured error",
            tool="get_rps",
            queries=queries,
            attempts=attempts,
        )
        return _error_response("Nessuna serie RPS trovata o metriche non disponibili")
    except Exception as exc:
        log_event("error", "Tool call failed", tool="get_rps", queries=queries, error=str(exc))
        return _error_response(str(exc))


@mcp.tool()
def get_current_replicas(deployment: str) -> dict[str, Any]:
    """Return the current replica count for a Kubernetes deployment."""

    log_event(
        "info",
        "Tool call started",
        tool="get_current_replicas",
        deployment=deployment,
    )
    try:
        if deployment not in STATE.allowed_deployments:
            return _error_response(
                f"Deployment '{deployment}' non consentito. Consentiti: {sorted(STATE.allowed_deployments)}"
            )

        if not STATE.kubernetes_ready or STATE.apps_api is None:
            message = STATE.kubernetes_error or "Kubernetes API non disponibile"
            log_event(
                "error",
                "Tool call failed",
                tool="get_current_replicas",
                deployment=deployment,
                error=message,
            )
            return _error_response(message)

        try:
            scale_obj = STATE.apps_api.read_namespaced_deployment_scale(
                name=deployment,
                namespace=STATE.namespace,
            )
        except client.exceptions.ApiException as api_exc:
            if api_exc.status == 403:
                message = (
                    f"Accesso negato (403) al deployment '{deployment}' nel namespace "
                    f"'{STATE.namespace}'. Probabile problema permessi RBAC."
                )
            elif api_exc.status == 404:
                message = (
                    f"Deployment '{deployment}' non trovato nel namespace '{STATE.namespace}' "
                    "(404 Not Found)."
                )
            else:
                message = (
                    f"Errore Kubernetes API durante lettura repliche per '{deployment}': "
                    f"status={api_exc.status}, reason={api_exc.reason}"
                )
            log_event(
                "error",
                "Tool call failed",
                tool="get_current_replicas",
                deployment=deployment,
                error=message,
            )
            return _error_response(message)

        replicas = int(scale_obj.status.replicas or 0)
        log_event(
            "info",
            "Tool call completed",
            tool="get_current_replicas",
            deployment=deployment,
            replicas=replicas,
        )
        return _success_response(replicas)
    except Exception as exc:
        log_event(
            "error",
            "Tool call failed",
            tool="get_current_replicas",
            deployment=deployment,
            error=str(exc),
        )
        return _error_response(str(exc))


@mcp.tool()
def set_replicas(deployment: str, replicas: int) -> dict[str, Any]:
    """Set the replica count of a Kubernetes deployment within guardrails."""

    log_event(
        "info",
        "Tool call started",
        tool="set_replicas",
        deployment=deployment,
        replicas=replicas,
    )
    try:
        if deployment not in STATE.allowed_deployments:
            return _error_response(
                f"Deployment '{deployment}' non consentito. Consentiti: {sorted(STATE.allowed_deployments)}"
            )
        if replicas < MIN_REPLICAS or replicas > MAX_REPLICAS:
            return _error_response(
                f"replicas={replicas} fuori limite. Limiti: min={MIN_REPLICAS}, max={MAX_REPLICAS}"
            )

        if not STATE.kubernetes_ready or STATE.apps_api is None:
            message = STATE.kubernetes_error or "Kubernetes API non disponibile"
            log_event(
                "error",
                "Tool call failed",
                tool="set_replicas",
                deployment=deployment,
                replicas=replicas,
                error=message,
            )
            return _error_response(message)

        body = {"spec": {"replicas": replicas}}
        try:
            STATE.apps_api.patch_namespaced_deployment_scale(
                name=deployment,
                namespace=STATE.namespace,
                body=body,
            )
        except client.exceptions.ApiException as api_exc:
            if api_exc.status == 403:
                message = (
                    f"Accesso negato (403) durante update del deployment '{deployment}' nel "
                    f"namespace '{STATE.namespace}'. Probabile problema permessi RBAC."
                )
            elif api_exc.status == 404:
                message = (
                    f"Deployment '{deployment}' non trovato nel namespace '{STATE.namespace}' "
                    "(404 Not Found)."
                )
            else:
                message = (
                    f"Errore Kubernetes API durante update repliche per '{deployment}': "
                    f"status={api_exc.status}, reason={api_exc.reason}"
                )
            log_event(
                "error",
                "Tool call failed",
                tool="set_replicas",
                deployment=deployment,
                replicas=replicas,
                error=message,
            )
            return _error_response(message)

        result_message = f"Repliche impostate a {replicas} per il deployment {deployment}"
        log_event(
            "info",
            "Tool call completed",
            tool="set_replicas",
            deployment=deployment,
            replicas=replicas,
        )
        return _success_response(result_message)
    except Exception as exc:
        log_event(
            "error",
            "Tool call failed",
            tool="set_replicas",
            deployment=deployment,
            replicas=replicas,
            error=str(exc),
        )
        return _error_response(str(exc))


def bootstrap_runtime() -> None:
    kubernetes_ready, kubernetes_error = _load_kubernetes_config()
    apps_api: client.AppsV1Api | None = None

    if kubernetes_ready:
        try:
            apps_api = client.AppsV1Api()
        except Exception as exc:
            kubernetes_ready = False
            kubernetes_error = f"Unable to initialize Kubernetes AppsV1Api: {exc}"
            log_event(
                "error",
                "Failed to initialize Kubernetes AppsV1Api",
                error=str(exc),
            )

    deployments = [item.strip() for item in TARGET_DEPLOYMENTS.split(",") if item.strip()]
    if not deployments:
        log_event("warning", "TARGET_DEPLOYMENTS is empty after parsing")

    STATE.apps_api = apps_api
    STATE.namespace = NAMESPACE
    STATE.prometheus_url = PROMETHEUS_URL
    STATE.kubernetes_ready = kubernetes_ready
    STATE.kubernetes_error = kubernetes_error
    STATE.allowed_deployments = set(deployments)

    log_event(
        "info",
        "MCP server initialized",
        namespace=NAMESPACE,
        target_deployments=deployments,
        prometheus_url=PROMETHEUS_URL,
        min_replicas=MIN_REPLICAS,
        max_replicas=MAX_REPLICAS,
        kubernetes_ready=kubernetes_ready,
        kubernetes_error=kubernetes_error,
    )


def main() -> None:
    try:
        bootstrap_runtime()
    except Exception as exc:
        log_event("error", "Runtime bootstrap failed", error=str(exc))

    try:
        mcp.run(transport="stdio")
    except Exception as exc:
        log_event("critical", "MCP server crashed", error=str(exc))


if __name__ == "__main__":
    main()