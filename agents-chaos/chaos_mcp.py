#!/usr/bin/env python3
import os
import random
import sys
from typing import Any

from kubernetes import client, config
from kubernetes.client import ApiException
from mcp.server.fastmcp import FastMCP

from structured_logger import get_logger


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
NAMESPACE = os.getenv("NAMESPACE", "default").strip()

LOGGER = get_logger("atlas-chaos-controller", stream=sys.stderr)
LOGGER.setLevel(LOG_LEVEL)

mcp = FastMCP("atlas-chaos-controller")

apps_api: client.AppsV1Api | None = None
core_api: client.CoreV1Api | None = None


def _error_response(message: str, data: Any = None) -> dict[str, Any]:
    return {"status": "error", "message": message, "data": data}


def _success_response(data: Any, message: str = "OK") -> dict[str, Any]:
    return {"status": "success", "message": message, "data": data}


def _ensure_clients() -> tuple[client.AppsV1Api, client.CoreV1Api]:
    if apps_api is None or core_api is None:
        raise RuntimeError("Kubernetes client is not initialized")
    return apps_api, core_api


def _init_kubernetes_client() -> tuple[client.AppsV1Api | None, client.CoreV1Api | None]:
    try:
        config.load_incluster_config()
        LOGGER.info(
            "Loaded in-cluster Kubernetes configuration",
            extra={"namespace": NAMESPACE},
        )
    except Exception as incluster_error:
        LOGGER.info(
            "In-cluster config unavailable, trying local kubeconfig",
            extra={"error": str(incluster_error)},
        )
        try:
            config.load_kube_config()
            LOGGER.info(
                "Loaded local kubeconfig",
                extra={"namespace": NAMESPACE},
            )
        except Exception as kubeconfig_error:
            LOGGER.error(
                "Failed to initialize Kubernetes configuration",
                extra={
                    "incluster_error": str(incluster_error),
                    "kubeconfig_error": str(kubeconfig_error),
                },
            )
            return None, None

    return client.AppsV1Api(), client.CoreV1Api()


apps_api, core_api = _init_kubernetes_client()


@mcp.tool()
def get_target_deployments() -> dict[str, Any]:
    """Return the list of deployments in the default namespace that can be targeted for chaos actions."""
    try:
        k8s_apps_api, _ = _ensure_clients()
        deployments = k8s_apps_api.list_namespaced_deployment(namespace=NAMESPACE)
        names = sorted(item.metadata.name for item in deployments.items if item.metadata and item.metadata.name)

        LOGGER.info(
            "Listed target deployments",
            extra={"namespace": NAMESPACE, "count": len(names)},
        )
        return _success_response(
            {
                "namespace": NAMESPACE,
                "deployments": names,
            }
        )
    except ApiException as exc:
        return _error_response(
            f"Kubernetes API error ({exc.status}): {exc.reason}",
            data={"namespace": NAMESPACE},
        )
    except Exception as exc:
        return _error_response(str(exc), data={"namespace": NAMESPACE})


@mcp.tool()
def kill_random_pod(deployment: str) -> dict[str, Any]:
    """Delete one random running pod from the target deployment to simulate a crash."""
    deployment = (deployment or "").strip()
    if not deployment:
        return _error_response("Parameter 'deployment' is required")

    try:
        k8s_apps_api, k8s_core_api = _ensure_clients()
        dep = k8s_apps_api.read_namespaced_deployment(name=deployment, namespace=NAMESPACE)

        selector = dep.spec.selector.match_labels if dep.spec and dep.spec.selector else None
        if not selector:
            return _error_response(
                "Deployment has no selector labels; cannot find related pods",
                data={"deployment": deployment, "namespace": NAMESPACE},
            )

        selector_str = ",".join(f"{k}={v}" for k, v in selector.items())
        pod_list = k8s_core_api.list_namespaced_pod(
            namespace=NAMESPACE,
            label_selector=selector_str,
        )

        running_pods = [
            pod
            for pod in pod_list.items
            if pod.metadata and pod.metadata.name and (pod.status and pod.status.phase == "Running")
        ]
        if not running_pods:
            return _error_response(
                "No running pods found for deployment",
                data={
                    "deployment": deployment,
                    "namespace": NAMESPACE,
                    "label_selector": selector_str,
                },
            )

        victim = random.choice(running_pods)
        victim_name = victim.metadata.name

        k8s_core_api.delete_namespaced_pod(name=victim_name, namespace=NAMESPACE)

        LOGGER.warning(
            "Deleted random pod for chaos simulation",
            extra={
                "deployment": deployment,
                "namespace": NAMESPACE,
                "pod": victim_name,
                "selector": selector_str,
            },
        )
        return _success_response(
            {
                "deployment": deployment,
                "namespace": NAMESPACE,
                "deleted_pod": victim_name,
                "label_selector": selector_str,
            },
            message="Pod deleted successfully",
        )
    except ApiException as exc:
        return _error_response(
            f"Kubernetes API error ({exc.status}): {exc.reason}",
            data={"deployment": deployment, "namespace": NAMESPACE},
        )
    except Exception as exc:
        return _error_response(
            str(exc),
            data={"deployment": deployment, "namespace": NAMESPACE},
        )


@mcp.tool()
def throttle_cpu_limits(deployment: str, cpu_limit: str = "50m") -> dict[str, Any]:
    """Patch deployment containers to reduce CPU limits, simulating starvation/noisy-neighbor pressure."""
    deployment = (deployment or "").strip()
    cpu_limit = (cpu_limit or "").strip()

    if not deployment:
        return _error_response("Parameter 'deployment' is required")
    if not cpu_limit:
        return _error_response("Parameter 'cpu_limit' is required")

    try:
        k8s_apps_api, _ = _ensure_clients()
        dep = k8s_apps_api.read_namespaced_deployment(name=deployment, namespace=NAMESPACE)

        containers = dep.spec.template.spec.containers if dep.spec and dep.spec.template and dep.spec.template.spec else []
        if not containers:
            return _error_response(
                "Deployment has no containers to patch",
                data={"deployment": deployment, "namespace": NAMESPACE},
            )

        patched_containers: list[dict[str, Any]] = []
        for c in containers:
            resources = c.resources.to_dict() if c.resources else {}
            limits = dict(resources.get("limits") or {})
            requests = dict(resources.get("requests") or {})
            previous_cpu_limit = limits.get("cpu")
            
            # FIX: Pareggiamo sia limits che requests verso il basso per evitare il 422
            limits["cpu"] = cpu_limit
            requests["cpu"] = "10m" # Un valore bassissimo per le requests

            patched_containers.append(
                {
                    "name": c.name,
                    "resources": {
                        "limits": limits,
                        "requests": requests,
                    },
                    "previous_cpu_limit": previous_cpu_limit,
                    "new_cpu_limit": cpu_limit,
                }
            )

        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": c["name"],
                                "resources": c["resources"],
                            }
                            for c in patched_containers
                        ]
                    }
                }
            }
        }

        k8s_apps_api.patch_namespaced_deployment(
            name=deployment,
            namespace=NAMESPACE,
            body=patch_body,
        )

        LOGGER.warning(
            "Throttled deployment CPU limits",
            extra={
                "deployment": deployment,
                "namespace": NAMESPACE,
                "cpu_limit": cpu_limit,
                "containers": [c["name"] for c in patched_containers],
            },
        )
        return _success_response(
            {
                "deployment": deployment,
                "namespace": NAMESPACE,
                "cpu_limit": cpu_limit,
                "patched_containers": patched_containers,
            },
            message="Deployment CPU limits patched successfully",
        )
    except ApiException as exc:
        LOGGER.error("Kubernetes API 422/Error during throttling", extra={"status": exc.status, "reason": exc.reason, "body": exc.body})
        return _error_response(
            f"Kubernetes API error ({exc.status}): {exc.reason}",
            data={
                "deployment": deployment,
                "namespace": NAMESPACE,
                "cpu_limit": cpu_limit,
            },
        )
    except Exception as exc:
        return _error_response(
            str(exc),
            data={
                "deployment": deployment,
                "namespace": NAMESPACE,
                "cpu_limit": cpu_limit,
            },
        )


if __name__ == "__main__":
    mcp.run(transport="stdio")
