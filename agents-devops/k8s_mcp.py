#!/usr/bin/env python3
import os
import sys
import re
import datetime
from typing import Any
from kubernetes import client, config
from mcp.server.fastmcp import FastMCP
from structured_logger import get_logger
from src.agent_guardian.budgeting import compute_budget_plan, parse_keyed_ints
from kubernetes.client.rest import ApiException

# Configuration and Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOGGER = get_logger("k8s-mcp-server", stream=sys.stderr)
LOGGER.setLevel(LOG_LEVEL)

# Operational Guardrails (Hardcoded Environment Variables)
NAMESPACE = os.getenv("NAMESPACE", "default").strip()
TARGET_DEPLOYMENTS = set(
    item.strip() for item in os.getenv(
        "TARGET_DEPLOYMENTS", 
        "api-gateway,scoring-system,enrichment-system,notification-system"
    ).split(",") if item.strip()
)
MIN_REPLICAS = int(os.getenv("MIN_REPLICAS", "1"))
BUDGET_MAX_REPLICAS = int(os.getenv("BUDGET_MAX_REPLICAS", "2"))
EMERGENCY_MAX_REPLICAS = int(
    os.getenv("EMERGENCY_MAX_REPLICAS", os.getenv("MAX_REPLICAS", "5"))
)
DEFAULT_TOTAL_REPLICA_BUDGET = max(BUDGET_MAX_REPLICAS * max(len(TARGET_DEPLOYMENTS), 1), MIN_REPLICAS)
TOTAL_REPLICA_BUDGET = int(os.getenv("TOTAL_REPLICA_BUDGET", str(DEFAULT_TOTAL_REPLICA_BUDGET)))

DEPLOYMENT_MIN_REPLICAS = parse_keyed_ints(
    os.getenv("DEPLOYMENT_MIN_REPLICAS", "").strip(),
    TARGET_DEPLOYMENTS,
    MIN_REPLICAS,
)
DEPLOYMENT_POD_COSTS = parse_keyed_ints(
    os.getenv("DEPLOYMENT_POD_COSTS", "").strip(),
    TARGET_DEPLOYMENTS,
    1,
)

if BUDGET_MAX_REPLICAS < MIN_REPLICAS:
    LOGGER.warning(
        "BUDGET_MAX_REPLICAS is lower than MIN_REPLICAS, clamping to MIN_REPLICAS",
        extra={"budget": BUDGET_MAX_REPLICAS, "min": MIN_REPLICAS},
    )
    BUDGET_MAX_REPLICAS = MIN_REPLICAS

if EMERGENCY_MAX_REPLICAS < BUDGET_MAX_REPLICAS:
    LOGGER.warning(
        "EMERGENCY_MAX_REPLICAS is lower than BUDGET_MAX_REPLICAS, clamping to budget",
        extra={"emergency": EMERGENCY_MAX_REPLICAS, "budget": BUDGET_MAX_REPLICAS},
    )
    EMERGENCY_MAX_REPLICAS = BUDGET_MAX_REPLICAS

if TOTAL_REPLICA_BUDGET < MIN_REPLICAS:
    LOGGER.warning(
        "TOTAL_REPLICA_BUDGET is lower than MIN_REPLICAS, clamping to MIN_REPLICAS",
        extra={"total_budget": TOTAL_REPLICA_BUDGET, "min": MIN_REPLICAS},
    )
    TOTAL_REPLICA_BUDGET = MIN_REPLICAS

for deployment in TARGET_DEPLOYMENTS:
    floor = DEPLOYMENT_MIN_REPLICAS.get(deployment, MIN_REPLICAS)
    if floor < MIN_REPLICAS:
        DEPLOYMENT_MIN_REPLICAS[deployment] = MIN_REPLICAS
    if DEPLOYMENT_POD_COSTS.get(deployment, 0) <= 0:
        DEPLOYMENT_POD_COSTS[deployment] = 1

# Kubernetes initialization
try:
    config.load_incluster_config()
    LOGGER.info("Loaded in-cluster K8s configuration")
except Exception:
    try:
        config.load_kube_config()
        LOGGER.info("Loaded local K8s configuration (fallback)")
    except Exception as e:
        LOGGER.critical(f"Unable to load K8s configuration: {e}")
        sys.exit(1)

apps_api = client.AppsV1Api()
autoscaling_api = client.AutoscalingV2Api()
mcp = FastMCP("atlas-k8s-controller")

def _error_response(msg: str, data: Any = None) -> dict[str, Any]:
    return {"status": "error", "message": msg, "data": data}

def _success_response(data: Any, msg: str = "OK") -> dict[str, Any]:
    return {"status": "success", "message": msg, "data": data}

def _read_all_current_replicas() -> dict[str, int]:
    replicas_by_deployment: dict[str, int] = {}
    for deployment in sorted(TARGET_DEPLOYMENTS):
        scale = apps_api.read_namespaced_deployment_scale(name=deployment, namespace=NAMESPACE)
        replicas_by_deployment[deployment] = scale.spec.replicas or 0
    return replicas_by_deployment

def _budget_usage(replicas_by_deployment: dict[str, int]) -> int:
    return sum(
        replicas_by_deployment.get(deployment, 0) * DEPLOYMENT_POD_COSTS[deployment]
        for deployment in TARGET_DEPLOYMENTS
    )

def _build_actions_from_plan(
    *,
    target_deployment: str,
    proposed_replicas: dict[str, int],
    donor_scale_down: dict[str, int],
    donor_priority: list[str] | None,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    donor_order = donor_priority or sorted(donor_scale_down)
    for donor in donor_order:
        if donor not in donor_scale_down:
            continue
        actions.append(
            {
                "deployment": donor,
                "action": "set_replicas",
                "new_replicas": proposed_replicas[donor],
            }
        )

    actions.append(
        {
            "deployment": target_deployment,
            "action": "set_replicas",
            "new_replicas": proposed_replicas[target_deployment],
        }
    )
    return actions

@mcp.tool()
def get_current_replicas(deployment: str) -> dict[str, Any]:
    """Read the current replica count and status of a Kubernetes deployment."""
    if deployment not in TARGET_DEPLOYMENTS:
        return _error_response(f"Access denied. Deployment '{deployment}' is not authorized.")

    try:
        scale = apps_api.read_namespaced_deployment_scale(name=deployment, namespace=NAMESPACE)
        status = apps_api.read_namespaced_deployment_status(name=deployment, namespace=NAMESPACE)
        
        data = {
            "configured_replicas": scale.spec.replicas,
            "available_replicas": status.status.available_replicas or 0,
            "unavailable_replicas": status.status.unavailable_replicas or 0
        }
        return _success_response(data)
    except client.exceptions.ApiException as e:
        return _error_response(f"K8s API error ({e.status}): {e.reason}")
    except Exception as e:
        return _error_response(f"Internal error: {str(e)}")

@mcp.tool()
def set_replicas(deployment: str, replicas: int) -> dict[str, Any]:
    """
    Modify the replica count of a deployment (scale up / scale down).
    Fails automatically if it violates safety limits (MIN_REPLICAS / EMERGENCY_MAX_REPLICAS).
    """
    if deployment not in TARGET_DEPLOYMENTS:
        return _error_response(f"Access denied. Deployment '{deployment}' is not authorized.")
    
    # Economic and safety guardrail
    if replicas < MIN_REPLICAS or replicas > EMERGENCY_MAX_REPLICAS:
        return _error_response(
            f"Action blocked by Safety Policy: {replicas} is outside the allowed limits "
            f"(Min: {MIN_REPLICAS}, Emergency max: {EMERGENCY_MAX_REPLICAS}). Request ignored."
        )

    # ECONOMIC BUDGET GUARD: prevent any direct scaling that would violate the global budget
    try:
        current = _read_all_current_replicas()
        projected = dict(current)
        projected[deployment] = int(replicas)
        projected_usage = _budget_usage(projected)
        if projected_usage > TOTAL_REPLICA_BUDGET:
            return _error_response(
                f"Action blocked by the Global Budget: the change would raise total usage to {projected_usage}, "
                f"above the allowed budget of {TOTAL_REPLICA_BUDGET}."
            )
    except Exception:
        # If budget check fails (unexpected), be conservative and block the action
        return _error_response("Unable to validate the global budget: action blocked for safety.")

    try:
        body = {"spec": {"replicas": replicas}}
        apps_api.patch_namespaced_deployment_scale(
            name=deployment, 
            namespace=NAMESPACE, 
            body=body
        )
        LOGGER.info(f"Scaling executed: {deployment} -> {replicas} replicas")
        return _success_response(
            {"deployment": deployment, "new_replicas": replicas},
            msg=f"Scaling completed successfully to {replicas} replicas."
        )
    except client.exceptions.ApiException as e:
        return _error_response(f"K8s API error ({e.status}) during scaling: {e.reason}")
    except Exception as e:
        return _error_response(f"Internal error during scaling: {str(e)}")

@mcp.tool()
def get_hpa_limits(deployment: str) -> dict[str, Any]:
    """Read the current HPA limits for the deployment and the allowed budget/emergency limits."""
    if deployment not in TARGET_DEPLOYMENTS:
        return _error_response(f"Access denied. Deployment '{deployment}' is not authorized.")

    try:
        hpa = autoscaling_api.read_namespaced_horizontal_pod_autoscaler(
            name=deployment,
            namespace=NAMESPACE,
        )
        data = {
            "deployment": deployment,
            "hpa_min_replicas": hpa.spec.min_replicas,
            "hpa_max_replicas": hpa.spec.max_replicas,
            "budget_max_replicas": BUDGET_MAX_REPLICAS,
            "emergency_max_replicas": EMERGENCY_MAX_REPLICAS,
        }
        return _success_response(data)
    except client.exceptions.ApiException as e:
        return _error_response(f"K8s API error ({e.status}) while reading HPA: {e.reason}")
    except Exception as e:
        return _error_response(f"Internal error while reading HPA: {str(e)}")

@mcp.tool()
def get_budget_state() -> dict[str, Any]:
    """Return the global budget state: current replicas, per-service costs, and remaining margin."""
    try:
        current = _read_all_current_replicas()
        current_usage = _budget_usage(current)
        data = {
            "namespace": NAMESPACE,
            "target_deployments": sorted(TARGET_DEPLOYMENTS),
            "total_replica_budget": TOTAL_REPLICA_BUDGET,
            "current_budget_usage": current_usage,
            "remaining_budget": TOTAL_REPLICA_BUDGET - current_usage,
            "deployment_pod_costs": dict(DEPLOYMENT_POD_COSTS),
            "deployment_min_replicas": dict(DEPLOYMENT_MIN_REPLICAS),
            "current_replicas": current,
        }
        return _success_response(data)
    except client.exceptions.ApiException as e:
        return _error_response(f"K8s API error ({e.status}) while reading budget: {e.reason}")
    except Exception as e:
        return _error_response(f"Internal error while reading budget: {str(e)}")

@mcp.tool()
def plan_budget_allocation(
    target_deployment: str,
    desired_replicas: int,
    donor_priority_csv: str = "",
) -> dict[str, Any]:
    """
    Compute a global reallocation plan under the shared budget constraint.
    Does not apply changes: returns only a verified proposal.
    """
    if target_deployment not in TARGET_DEPLOYMENTS:
        return _error_response(f"Access denied. Deployment '{target_deployment}' is not authorized.")

    floor = DEPLOYMENT_MIN_REPLICAS[target_deployment]
    if desired_replicas < floor or desired_replicas > EMERGENCY_MAX_REPLICAS:
        return _error_response(
            f"Action blocked by Safety Policy: desired_replicas={desired_replicas} is outside the limits "
            f"(Service min: {floor}, Emergency max: {EMERGENCY_MAX_REPLICAS})."
        )

    try:
        current = _read_all_current_replicas()
        donor_priority = [item.strip() for item in donor_priority_csv.split(",") if item.strip()]
        plan = compute_budget_plan(
            current_replicas=current,
            target_deployment=target_deployment,
            desired_replicas=desired_replicas,
            total_budget=TOTAL_REPLICA_BUDGET,
            pod_costs=DEPLOYMENT_POD_COSTS,
            floors=DEPLOYMENT_MIN_REPLICAS,
            donor_priority=donor_priority or None,
        )

        if not plan.get("feasible", False):
            return _error_response(
                f"Plan is not feasible within the global budget: {plan.get('reason', 'constraint not satisfied')}"
            )

        donor_scale_down = plan.get("donor_scale_down") or {}
        actions = _build_actions_from_plan(
            target_deployment=target_deployment,
            proposed_replicas=plan["proposed_replicas"],
            donor_scale_down=donor_scale_down,
            donor_priority=donor_priority or None,
        )

        data = {
            "target_deployment": target_deployment,
            "desired_replicas": desired_replicas,
            "current_replicas": plan["current_replicas"],
            "proposed_replicas": plan["proposed_replicas"],
            "deployment_pod_costs": dict(DEPLOYMENT_POD_COSTS),
            "deployment_min_replicas": dict(DEPLOYMENT_MIN_REPLICAS),
            "current_budget_usage": plan["current_budget_usage"],
            "resulting_budget_usage": plan["resulting_budget_usage"],
            "total_budget": plan["total_budget"],
            "remaining_budget_after": plan["total_budget"] - plan["resulting_budget_usage"],
            "donor_scale_down": donor_scale_down,
            "actions_in_order": actions,
        }
        return _success_response(data, msg="Global budget plan computed successfully.")
    except client.exceptions.ApiException as e:
        return _error_response(f"K8s API error ({e.status}) during budget planning: {e.reason}")
    except Exception as e:
        return _error_response(f"Internal error during budget planning: {str(e)}")

@mcp.tool()
def execute_budget_allocation(
    target_deployment: str,
    desired_replicas: int,
    donor_priority_csv: str = "",
) -> dict[str, Any]:
    """
    Execute a replica reallocation with the shared global budget.
    Safe order: scale donors down first, then scale the target up.
    If a step fails, attempt rollback of already applied changes.
    """
    if target_deployment not in TARGET_DEPLOYMENTS:
        return _error_response(f"Access denied. Deployment '{target_deployment}' is not authorized.")

    floor = DEPLOYMENT_MIN_REPLICAS[target_deployment]
    if desired_replicas < floor or desired_replicas > EMERGENCY_MAX_REPLICAS:
        return _error_response(
            f"Action blocked by Safety Policy: desired_replicas={desired_replicas} is outside the limits "
            f"(Service min: {floor}, Emergency max: {EMERGENCY_MAX_REPLICAS})."
        )

    try:
        current = _read_all_current_replicas()
        donor_priority = [item.strip() for item in donor_priority_csv.split(",") if item.strip()]
        plan = compute_budget_plan(
            current_replicas=current,
            target_deployment=target_deployment,
            desired_replicas=desired_replicas,
            total_budget=TOTAL_REPLICA_BUDGET,
            pod_costs=DEPLOYMENT_POD_COSTS,
            floors=DEPLOYMENT_MIN_REPLICAS,
            donor_priority=donor_priority or None,
        )
        if not plan.get("feasible", False):
            return _error_response(
                f"Budget execution is not feasible: {plan.get('reason', 'constraint not satisfied')}"
            )

        proposed = plan["proposed_replicas"]
        donor_scale_down = plan.get("donor_scale_down") or {}
        actions = _build_actions_from_plan(
            target_deployment=target_deployment,
            proposed_replicas=proposed,
            donor_scale_down=donor_scale_down,
            donor_priority=donor_priority or None,
        )

        # Track only successful changes for rollback in reverse order.
        applied_changes: list[dict[str, Any]] = []
        for step in actions:
            deployment = step["deployment"]
            new_replicas = int(step["new_replicas"])
            old_replicas = current[deployment]
            if new_replicas == old_replicas:
                continue

            result = set_replicas(deployment=deployment, replicas=new_replicas)
            if result.get("status") != "success":
                rollback_results: list[dict[str, Any]] = []
                for change in reversed(applied_changes):
                    rollback = set_replicas(
                        deployment=change["deployment"],
                        replicas=change["old_replicas"],
                    )
                    rollback_results.append(
                        {
                            "deployment": change["deployment"],
                            "target_replicas": change["old_replicas"],
                            "status": rollback.get("status", "error"),
                            "message": rollback.get("message", ""),
                        }
                    )

                return _error_response(
                    "Partial execution failed: rollback attempted.",
                    data={
                        "target_deployment": target_deployment,
                        "desired_replicas": desired_replicas,
                        "current_replicas": current,
                        "planned_actions": actions,
                        "applied_changes": applied_changes,
                        "failed_step": {
                            "deployment": deployment,
                            "new_replicas": new_replicas,
                            "error": result.get("message", "unknown"),
                        },
                        "rollback_attempted": bool(applied_changes),
                        "rollback_results": rollback_results,
                    },
                )

            applied_changes.append(
                {
                    "deployment": deployment,
                    "old_replicas": old_replicas,
                    "new_replicas": new_replicas,
                }
            )

        final_state = _read_all_current_replicas()
        final_usage = _budget_usage(final_state)
        data = {
            "target_deployment": target_deployment,
            "desired_replicas": desired_replicas,
            "initial_replicas": current,
            "final_replicas": final_state,
            "deployment_pod_costs": dict(DEPLOYMENT_POD_COSTS),
            "deployment_min_replicas": dict(DEPLOYMENT_MIN_REPLICAS),
            "total_budget": TOTAL_REPLICA_BUDGET,
            "final_budget_usage": final_usage,
            "remaining_budget": TOTAL_REPLICA_BUDGET - final_usage,
            "planned_actions": actions,
            "applied_changes": applied_changes,
            "rollback_attempted": False,
        }
        return _success_response(data, msg="Budget reallocation executed successfully.")
    except client.exceptions.ApiException as e:
        return _error_response(f"K8s API error ({e.status}) during budget execution: {e.reason}")
    except Exception as e:
        return _error_response(f"Internal error during budget execution: {str(e)}")

@mcp.tool()
def set_hpa_max_replicas(deployment: str, max_replicas: int) -> dict[str, Any]:
    """
    Temporarily set the deployment HPA maximum.
    Allowed only within [BUDGET_MAX_REPLICAS, EMERGENCY_MAX_REPLICAS].
    """
    if deployment not in TARGET_DEPLOYMENTS:
        return _error_response(f"Access denied. Deployment '{deployment}' is not authorized.")

    # Do not allow raising HPA max above per-service budget. Emergency increases must use
    # an explicit, audited path (e.g., operator intervention). This prevents the HPA
    # from autonomously scaling a deployment beyond the global shared budget.
    if max_replicas < BUDGET_MAX_REPLICAS or max_replicas > EMERGENCY_MAX_REPLICAS:
        return _error_response(
            f"Action blocked by Safety Policy: max_replicas={max_replicas} is outside the allowed limits "
            f"(Budget: {BUDGET_MAX_REPLICAS}, Emergency max: {EMERGENCY_MAX_REPLICAS})."
        )

    if max_replicas > BUDGET_MAX_REPLICAS:
        return _error_response(
            f"Action blocked: raising the HPA above the per-service budget limit is not allowed ({BUDGET_MAX_REPLICAS})."
        )

    try:
        current = autoscaling_api.read_namespaced_horizontal_pod_autoscaler(
            name=deployment,
            namespace=NAMESPACE,
        )
        current_min = current.spec.min_replicas or MIN_REPLICAS
        current_max = current.spec.max_replicas

        if max_replicas < current_min:
            return _error_response(
                f"Action blocked: max_replicas={max_replicas} is below the current minReplicas ({current_min})."
            )

        body = {"spec": {"maxReplicas": max_replicas}}
        autoscaling_api.patch_namespaced_horizontal_pod_autoscaler(
            name=deployment,
            namespace=NAMESPACE,
            body=body,
        )

        LOGGER.info(
            "Updated HPA limit",
            extra={
                "deployment": deployment,
                "old_max_replicas": current_max,
                "new_max_replicas": max_replicas,
            },
        )

        return _success_response(
            {
                "deployment": deployment,
                "old_hpa_max_replicas": current_max,
                "new_hpa_max_replicas": max_replicas,
                "budget_max_replicas": BUDGET_MAX_REPLICAS,
                "emergency_max_replicas": EMERGENCY_MAX_REPLICAS,
            },
            msg=f"HPA maxReplicas updated successfully to {max_replicas}.",
        )
    except client.exceptions.ApiException as e:
        return _error_response(f"K8s API error ({e.status}) during HPA update: {e.reason}")
    except Exception as e:
        return _error_response(f"Internal error during HPA update: {str(e)}")


@mcp.tool()
def set_hpa_max_replicas_temporary(
    deployment: str,
    max_replicas: int,
    duration_seconds: int = 600,
    cpu_down_threshold: int = 50,
) -> dict[str, Any]:
    """
    Temporarily raise the HPA `maxReplicas` for `deployment`.
    Adds annotations to the HPA recording the original value and expiry.
    The annotation keys are: `guardian.temp_orig`, `guardian.temp_until`, `guardian.temp_cpu_threshold`.
    A separate check/revert tool will remove the temporary setting when traffic drops or when expired.
    """
    if deployment not in TARGET_DEPLOYMENTS:
        return _error_response(f"Access denied. Deployment '{deployment}' is not authorized.")

    try:
        hpa = autoscaling_api.read_namespaced_horizontal_pod_autoscaler(name=deployment, namespace=NAMESPACE)
        annotations = dict(hpa.metadata.annotations or {})
        orig_max = int(annotations.get("guardian.temp_orig") or hpa.spec.max_replicas)

        # Patch HPA maxReplicas
        body = {"spec": {"maxReplicas": int(max_replicas)}}
        autoscaling_api.patch_namespaced_horizontal_pod_autoscaler(name=deployment, namespace=NAMESPACE, body=body)

        # Annotate HPA with temp metadata
        until = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=int(duration_seconds))).isoformat()
        annotations["guardian.temp_orig"] = str(orig_max)
        annotations["guardian.temp_until"] = until
        annotations["guardian.temp_cpu_threshold"] = str(int(cpu_down_threshold))

        patch_body = {"metadata": {"annotations": annotations}}
        try:
            autoscaling_api.patch_namespaced_horizontal_pod_autoscaler(name=deployment, namespace=NAMESPACE, body=patch_body)
        except Exception:
            autoscaling_api.patch_namespaced_horizontal_pod_autoscaler(
                name=deployment,
                namespace=NAMESPACE,
                body={"spec": {"maxReplicas": orig_max}},
            )
            raise

        LOGGER.info("Temporarily raised HPA maxReplicas", extra={"deployment": deployment, "new_max": max_replicas, "orig_max": orig_max, "until": until})
        return _success_response({"deployment": deployment, "new_max": int(max_replicas), "orig_max": orig_max, "until": until}, msg="Temporary HPA maxReplicas set")
    except client.exceptions.ApiException as exc:
        return _error_response(f"K8s API error: {exc.reason} ({exc.status})")
    except Exception as exc:
        return _error_response(f"Internal error: {str(exc)}")


@mcp.tool()
def check_and_revert_temp_hpa(deployment: str | None = None) -> dict[str, Any]:
    """
    Scan either a single `deployment` HPA or all HPAs and revert any temporary maxReplicas
    if either the expiry timestamp has passed or current CPU utilization is below the configured threshold.
    Returns list of actions taken.
    """
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    def _parse_temp_timestamp(value: Any) -> datetime.datetime:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("missing timestamp")
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone(datetime.timezone.utc)

    def _safe_int(value: Any, fallback: int) -> int:
        try:
            return int(value)
        except Exception:
            return fallback

    def _current_cpu_utilization(hpa: Any) -> int | None:
        status = getattr(hpa, "status", None)
        current_metrics = getattr(status, "current_metrics", None) or getattr(status, "currentMetrics", None) or []
        for metric in current_metrics:
            resource = getattr(metric, "resource", None)
            if resource is None:
                continue
            current = getattr(resource, "current", None)
            if current is None:
                continue
            for attr in ("average_utilization", "averageUtilization"):
                value = getattr(current, attr, None)
                if value is None:
                    continue
                try:
                    return int(value)
                except Exception:
                    continue
        return None

    try:
        hpa_list = [autoscaling_api.read_namespaced_horizontal_pod_autoscaler(name=deployment, namespace=NAMESPACE)] if deployment else autoscaling_api.list_namespaced_horizontal_pod_autoscaler(namespace=NAMESPACE).items

        now = datetime.datetime.now(datetime.timezone.utc)
        for hpa in hpa_list:
            name = hpa.metadata.name
            ann = hpa.metadata.annotations or {}
            if not ann.get("guardian.temp_until"):
                continue

            try:
                orig = _safe_int(ann.get("guardian.temp_orig"), int(hpa.spec.max_replicas or 0))
                until = _parse_temp_timestamp(ann.get("guardian.temp_until"))
                cpu_thr = _safe_int(ann.get("guardian.temp_cpu_threshold"), 50)
            except Exception as exc:
                errors.append({"hpa": name, "error": f"invalid temp annotations: {exc}"})
                continue

            should_revert = False
            reason = ""
            if now >= until:
                should_revert = True
                reason = "expired"
            else:
                cur_val = _current_cpu_utilization(hpa)
                if cur_val is not None and cur_val < cpu_thr:
                    should_revert = True
                    reason = f"cpu_below_threshold:{cur_val}<{cpu_thr}"

            if should_revert:
                try:
                    # Revert hpa.spec.maxReplicas first so the workload can scale back down naturally.
                    autoscaling_api.patch_namespaced_horizontal_pod_autoscaler(
                        name=name,
                        namespace=NAMESPACE,
                        body={"spec": {"maxReplicas": orig}},
                    )

                    # Remove annotations we set; if this fails, keep the restored spec and report a partial revert.
                    new_ann = dict(ann)
                    for k in ["guardian.temp_orig", "guardian.temp_until", "guardian.temp_cpu_threshold"]:
                        new_ann.pop(k, None)
                    autoscaling_api.patch_namespaced_horizontal_pod_autoscaler(
                        name=name,
                        namespace=NAMESPACE,
                        body={"metadata": {"annotations": new_ann}},
                    )
                    results.append({"hpa": name, "action": "reverted", "reverted_to": orig, "reason": reason})
                except Exception as exc:
                    errors.append({"hpa": name, "error": f"revert failed after decision {reason}: {exc}"})

        payload: dict[str, Any] = {"reverted": results}
        if errors:
            payload["errors"] = errors
        msg = "Checked temporary HPAs"
        if errors:
            msg = "Checked temporary HPAs with partial errors"
        return _success_response(payload, msg=msg)
    except client.exceptions.ApiException as exc:
        return _error_response(f"K8s API error: {exc.reason} ({exc.status})")
    except Exception as exc:
        return _error_response(f"Internal error: {str(exc)}")


@mcp.tool()
def list_temporary_hpas() -> dict[str, Any]:
    """List HPAs annotated as temporary by the guardian."""
    found: list[dict[str, Any]] = []
    try:
        items = autoscaling_api.list_namespaced_horizontal_pod_autoscaler(namespace=NAMESPACE).items
        for hpa in items:
            ann = hpa.metadata.annotations or {}
            if ann.get("guardian.temp_until"):
                found.append({"name": hpa.metadata.name, "annotations": ann, "spec_max": hpa.spec.max_replicas, "status": getattr(hpa.status, 'current_replicas', None)})
        return _success_response(found)
    except client.exceptions.ApiException as exc:
        return _error_response(f"K8s API error: {exc.reason} ({exc.status})")
    except Exception as exc:
        return _error_response(f"Internal error: {str(exc)}")

@mcp.tool()
def get_deployment_resources(deployment: str, namespace: str = "default") -> str:
    """Return the current CPU and RAM requests/limits for a deployment."""
    clean_name = re.sub(r'-[0-9]+$', '', deployment)
    try:
        apps_v1 = client.AppsV1Api()
        try:
            workload = apps_v1.read_namespaced_deployment(name=clean_name, namespace=namespace)
            w_type = "Deployment"
        except ApiException as e:
            if e.status == 404:
                workload = apps_v1.read_namespaced_stateful_set(name=clean_name, namespace=namespace)
                w_type = "StatefulSet"
            else:
                raise e
        
        containers = workload.spec.template.spec.containers
        report = [f"Type: {w_type}"]
        for c in containers:
            resources = c.resources
            limits = resources.limits if resources.limits else "Not set"
            requests = resources.requests if resources.requests else "Not set"
            report.append(f"Container '{c.name}': Limits {limits}, Requests {requests}")
        return " | ".join(report)
    except ApiException as e:
        if e.status == 404:
            return f"No Deployment or StatefulSet found with name '{clean_name}'"
        return f"K8s API access error: {e.reason} ({e.status})"
    except Exception as e:
        return f"Internal tool error: {str(e)}"

@mcp.tool()
def restore_cpu_limits(deployment: str) -> dict[str, Any]:
    """Restore the CPU limits of a deployment or statefulset to normal healthy values (requests: 50m, limits: 250m) to mitigate CPU starvation/throttling."""
    deployment = (deployment or "").strip()
    if not deployment:
        return _error_response("Parameter 'deployment' is required")

    clean_name = re.sub(r'-[0-9]+$', '', deployment)
    w_type = "Deployment"

    try:
        try:
            workload = apps_api.read_namespaced_deployment(name=clean_name, namespace=NAMESPACE)
            w_type = "Deployment"
        except ApiException as e:
            if e.status == 404:
                workload = apps_api.read_namespaced_stateful_set(name=clean_name, namespace=NAMESPACE)
                w_type = "StatefulSet"
            else:
                raise e

        containers = workload.spec.template.spec.containers if workload.spec and workload.spec.template and workload.spec.template.spec else []
        if not containers:
            return _error_response(f"{w_type} has no containers to patch", data={"deployment": clean_name})

        safe_requests = "50m"
        safe_limits = "250m"

        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": c.name,
                                "resources": {
                                    "requests": {"cpu": safe_requests},
                                    "limits": {"cpu": safe_limits}
                                }
                            }
                            for c in containers
                        ]
                    }
                }
            }
        }

        if w_type == "Deployment":
            apps_api.patch_namespaced_deployment(
                name=clean_name,
                namespace=NAMESPACE,
                body=patch_body,
            )
        else:
            apps_api.patch_namespaced_stateful_set(
                name=clean_name,
                namespace=NAMESPACE,
                body=patch_body,
            )

        LOGGER.info(
            f"Restored {w_type} CPU limits to healthy defaults",
            extra={
                "deployment": clean_name,
                "type": w_type,
                "namespace": NAMESPACE,
                "requests": safe_requests,
                "limits": safe_limits
            },
        )
        return _success_response(
            {
                "deployment": clean_name,
                "type": w_type,
                "status": "restored",
                "cpu_requests": safe_requests,
                "cpu_limits": safe_limits
            },
            msg=f"CPU limits restored successfully for {w_type} '{clean_name}'"
        )
    except ApiException as exc:
        if exc.status == 404:
            return _error_response(f"No Deployment or StatefulSet found with name '{clean_name}'", data={"deployment": deployment})
        return _error_response(f"Kubernetes API error: {exc.reason} ({exc.status})", data={"deployment": deployment})
    except Exception as exc:
        return _error_response(str(exc), data={"deployment": deployment})

@mcp.tool()
def get_workload_health(workload_query: str) -> str:
    """Search workloads by partial or exact name and return pod health (phase, age, readiness, restarts)."""
    query = (workload_query or "").strip().lower()
    
    try:
        core_api = client.CoreV1Api()
        apps_api = client.AppsV1Api()
        
        matched_workloads = []
        
        # 1. Retrieve all Deployments and StatefulSets
        deployments = apps_api.list_namespaced_deployment(namespace=NAMESPACE)
        for d in deployments.items:
            if query in d.metadata.name.lower():
                matched_workloads.append((d.metadata.name, "Deployment", d.spec.selector.match_labels))
                
        statefulsets = apps_api.list_namespaced_stateful_set(namespace=NAMESPACE)
        for s in statefulsets.items:
            if query in s.metadata.name.lower():
                matched_workloads.append((s.metadata.name, "StatefulSet", s.spec.selector.match_labels))

        if not matched_workloads:
            return f"No workload found matching query '{query}'."

        report = []
        now = datetime.datetime.now(datetime.timezone.utc)
        
        # 2. For each match, query Pods using the label selector
        for name, w_type, labels_dict in matched_workloads:
            if not labels_dict:
                continue
                
            label_selector = ",".join([f"{k}={v}" for k, v in labels_dict.items()])
            pods = core_api.list_namespaced_pod(namespace=NAMESPACE, label_selector=label_selector)
            
            report.append(f"\n--- Status for {w_type}: {name} ---")
            if not pods.items:
                report.append("No active Pods found (possible scale-to-zero).")
                continue
                
            for p in pods.items:
                p_name = p.metadata.name
                phase = p.status.phase
                age_mins = int((now - p.metadata.creation_timestamp).total_seconds() / 60)
                
                ready_count = sum(1 for c in (p.status.container_statuses or []) if c.ready)
                total_count = len(p.spec.containers)
                restarts = sum(c.restart_count for c in (p.status.container_statuses or []))
                
                status_details = ""
                for c in (p.status.container_statuses or []):
                    if c.state.waiting:
                        status_details += f"[{c.name} waiting: {c.state.waiting.reason}] "
                
                report.append(
                    f"- Pod: {p_name} | Phase: {phase} | Age: {age_mins}m | "
                    f"Ready: {ready_count}/{total_count} | Restarts: {restarts} | {status_details.strip()}"
                )
                
        return "\n".join(report)

    except Exception as exc:
        return f"K8s API error during search: {str(exc)}"

if __name__ == "__main__":
    mcp.run(transport="stdio")