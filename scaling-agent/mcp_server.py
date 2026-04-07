#!/usr/bin/env python3
"""ATLAS MCP server for the all-in-one scaling agent runtime.

This process is spawned locally by mcp_client.py over stdio and exposes the
Prometheus/Kubernetes tools needed by the LLM loop.
"""

import os
import sys
from dataclasses import dataclass
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


def _load_kubernetes_config() -> None:
    try:
        config.load_incluster_config()
        log_event("info", "Loaded in-cluster Kubernetes config")
    except Exception:
        config.load_kube_config()
        log_event("warning", "Loaded local kube config as fallback")


def _run_prometheus_query(prometheus_base_url: str, query: str) -> list[dict[str, Any]]:
    endpoint = f"{prometheus_base_url.rstrip('/')}/api/v1/query"
    response = requests.get(endpoint, params={"query": query}, timeout=10)
    response.raise_for_status()
    data = response.json()

    if data.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed for '{query}': {data}")

    result = data.get("data", {}).get("result", [])
    if not isinstance(result, list):
        raise RuntimeError(f"Invalid Prometheus result format for '{query}'")
    return result


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
class RuntimeContext:
    apps_api: client.AppsV1Api
    namespace: str
    prometheus_url: str


RUNTIME_CONTEXT: RuntimeContext | None = None
ALLOWED_DEPLOYMENTS: set[str] = set()
mcp = FastMCP("atlas-aiops-server")


def _get_runtime_context() -> RuntimeContext:
    if RUNTIME_CONTEXT is None:
        raise RuntimeError("Runtime context is not initialized")
    return RUNTIME_CONTEXT


@mcp.tool()
def get_scaling_recommendation(deployment: str) -> dict[str, Any]:
    """Return the current traffic-based scaling recommendation for a deployment."""

    if deployment not in ALLOWED_DEPLOYMENTS:
        raise ValueError(
            f"Deployment '{deployment}' non consentito. Consentiti: {sorted(ALLOWED_DEPLOYMENTS)}"
        )

    thresholds = _parse_rps_thresholds(RPS_REPLICA_THRESHOLDS)
    observed_rps = get_rps()
    current = get_current_replicas(deployment=deployment)
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
    log_event("info", "Tool call completed", tool="get_scaling_recommendation", **recommendation)
    return recommendation


@mcp.tool()
def get_rps() -> float:
    """Read the global RPS from Prometheus, with a fallback query for live traffic."""

    context = _get_runtime_context()
    queries = [PROMQL_RPS_QUERY, *PROMQL_RPS_FALLBACK_QUERIES]
    attempts: list[dict[str, str]] = []

    log_event("info", "Tool call started", tool="get_rps")
    for query in queries:
        result = _run_prometheus_query(context.prometheus_url, query)
        if not result:
            attempts.append({"query": query, "status": "empty_result"})
            continue

        value = result[0].get("value", [None, "0"])[1]
        rps = float(value)
        log_event(
            "info",
            "Tool call completed",
            tool="get_rps",
            query=query,
            rps=round(rps, 6),
        )
        return rps

    log_event("error", "Tool call failed", tool="get_rps", attempts=attempts)
    log_event(
        "warning",
        "No RPS series found for configured queries; defaulting to 0",
        tool="get_rps",
        attempts=attempts,
    )
    return 0.0


@mcp.tool()
def get_current_replicas(deployment: str) -> int:
    """Return the current replica count for a Kubernetes deployment."""

    if deployment not in ALLOWED_DEPLOYMENTS:
        raise ValueError(
            f"Deployment '{deployment}' non consentito. Consentiti: {sorted(ALLOWED_DEPLOYMENTS)}"
        )

    context = _get_runtime_context()
    log_event("info", "Tool call started", tool="get_current_replicas", deployment=deployment)
    scale_obj = context.apps_api.read_namespaced_deployment_scale(
        name=deployment,
        namespace=context.namespace,
    )
    replicas = int(scale_obj.status.replicas or 0)
    log_event(
        "info",
        "Tool call completed",
        tool="get_current_replicas",
        deployment=deployment,
        replicas=replicas,
    )
    return replicas


@mcp.tool()
def set_replicas(deployment: str, replicas: int) -> str:
    """Set the replica count of a Kubernetes deployment within guardrails."""

    if deployment not in ALLOWED_DEPLOYMENTS:
        raise ValueError(
            f"Deployment '{deployment}' non consentito. Consentiti: {sorted(ALLOWED_DEPLOYMENTS)}"
        )
    if replicas < MIN_REPLICAS or replicas > MAX_REPLICAS:
        raise ValueError(
            f"replicas={replicas} fuori limite. Limiti: min={MIN_REPLICAS}, max={MAX_REPLICAS}"
        )

    context = _get_runtime_context()
    body = {"spec": {"replicas": replicas}}

    log_event(
        "info",
        "Tool call started",
        tool="set_replicas",
        deployment=deployment,
        requested_replicas=replicas,
    )
    context.apps_api.patch_namespaced_deployment_scale(
        name=deployment,
        namespace=context.namespace,
        body=body,
    )

    result_message = f"Repliche impostate a {replicas} per il deployment {deployment}"
    log_event(
        "info",
        "Tool call completed",
        tool="set_replicas",
        deployment=deployment,
        replicas=replicas,
    )
    return result_message


def bootstrap_runtime() -> None:
    _load_kubernetes_config()
    apps_api = client.AppsV1Api()

    deployments = [item.strip() for item in TARGET_DEPLOYMENTS.split(",") if item.strip()]
    if not deployments:
        raise RuntimeError("TARGET_DEPLOYMENTS is empty after parsing")

    global RUNTIME_CONTEXT
    global ALLOWED_DEPLOYMENTS
    RUNTIME_CONTEXT = RuntimeContext(
        apps_api=apps_api,
        namespace=NAMESPACE,
        prometheus_url=PROMETHEUS_URL,
    )
    ALLOWED_DEPLOYMENTS = set(deployments)

    log_event(
        "info",
        "MCP server initialized",
        namespace=NAMESPACE,
        target_deployments=deployments,
        prometheus_url=PROMETHEUS_URL,
        min_replicas=MIN_REPLICAS,
        max_replicas=MAX_REPLICAS,
    )


def main() -> None:
    bootstrap_runtime()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()