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

# Configurazione e Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOGGER = get_logger("k8s-mcp-server", stream=sys.stderr)
LOGGER.setLevel(LOG_LEVEL)

# Guardrail Operativi (Hardcoded Environment Variables)
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

# Inizializzazione Kubernetes
try:
    config.load_incluster_config()
    LOGGER.info("Caricata configurazione K8s in-cluster")
except Exception:
    try:
        config.load_kube_config()
        LOGGER.info("Caricata configurazione K8s locale (fallback)")
    except Exception as e:
        LOGGER.critical(f"Impossibile caricare la configurazione K8s: {e}")
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
    """Legge il numero di repliche correnti e lo stato di un deployment su Kubernetes."""
    if deployment not in TARGET_DEPLOYMENTS:
        return _error_response(f"Accesso negato. Deployment '{deployment}' non autorizzato.")

    try:
        scale = apps_api.read_namespaced_deployment_scale(name=deployment, namespace=NAMESPACE)
        status = apps_api.read_namespaced_deployment_status(name=deployment, namespace=NAMESPACE)
        
        data = {
            "replicas_configurate": scale.spec.replicas,
            "replicas_disponibili": status.status.available_replicas or 0,
            "replicas_non_pronte": status.status.unavailable_replicas or 0
        }
        return _success_response(data)
    except client.exceptions.ApiException as e:
        return _error_response(f"Errore K8s API ({e.status}): {e.reason}")
    except Exception as e:
        return _error_response(f"Errore interno: {str(e)}")

@mcp.tool()
def set_replicas(deployment: str, replicas: int) -> dict[str, Any]:
    """
    Modifica il numero di repliche di un deployment (Scale Up / Scale Down).
    Fallisce automaticamente se viola i limiti di sicurezza (MIN_REPLICAS / EMERGENCY_MAX_REPLICAS).
    """
    if deployment not in TARGET_DEPLOYMENTS:
        return _error_response(f"Accesso negato. Deployment '{deployment}' non autorizzato.")
    
    # GUARDRAIL ECONOMICO E DI SICUREZZA
    if replicas < MIN_REPLICAS or replicas > EMERGENCY_MAX_REPLICAS:
        return _error_response(
            f"Azione bloccata dalla Safety Policy: {replicas} è fuori dai limiti consentiti "
            f"(Min: {MIN_REPLICAS}, Max emergenza: {EMERGENCY_MAX_REPLICAS}). Richiesta ignorata."
        )

    # ECONOMIC BUDGET GUARD: prevent any direct scaling that would violate the global budget
    try:
        current = _read_all_current_replicas()
        projected = dict(current)
        projected[deployment] = int(replicas)
        projected_usage = _budget_usage(projected)
        if projected_usage > TOTAL_REPLICA_BUDGET:
            return _error_response(
                f"Azione bloccata dal Budget Globale: la modifica porta l'uso totale a {projected_usage}, "
                f"sopra il budget consentito di {TOTAL_REPLICA_BUDGET}."
            )
    except Exception:
        # If budget check fails (unexpected), be conservative and block the action
        return _error_response("Impossibile validare il budget globale: azione bloccata per sicurezza.")

    try:
        body = {"spec": {"replicas": replicas}}
        apps_api.patch_namespaced_deployment_scale(
            name=deployment, 
            namespace=NAMESPACE, 
            body=body
        )
        LOGGER.info(f"Eseguito scaling: {deployment} -> {replicas} repliche")
        return _success_response(
            {"deployment": deployment, "new_replicas": replicas},
            msg=f"Scaling completato con successo a {replicas} repliche."
        )
    except client.exceptions.ApiException as e:
        return _error_response(f"Errore K8s API ({e.status}) durante lo scaling: {e.reason}")
    except Exception as e:
        return _error_response(f"Errore interno durante lo scaling: {str(e)}")

@mcp.tool()
def get_hpa_limits(deployment: str) -> dict[str, Any]:
    """Legge i limiti HPA correnti del deployment e i limiti budget/emergenza consentiti."""
    if deployment not in TARGET_DEPLOYMENTS:
        return _error_response(f"Accesso negato. Deployment '{deployment}' non autorizzato.")

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
        return _error_response(f"Errore K8s API ({e.status}) durante lettura HPA: {e.reason}")
    except Exception as e:
        return _error_response(f"Errore interno durante lettura HPA: {str(e)}")

@mcp.tool()
def get_budget_state() -> dict[str, Any]:
    """Restituisce stato budget globale: repliche correnti, costi per servizio e margine residuo."""
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
        return _error_response(f"Errore K8s API ({e.status}) durante lettura budget: {e.reason}")
    except Exception as e:
        return _error_response(f"Errore interno durante lettura budget: {str(e)}")

@mcp.tool()
def plan_budget_allocation(
    target_deployment: str,
    desired_replicas: int,
    donor_priority_csv: str = "",
) -> dict[str, Any]:
    """
    Calcola un piano di riallocazione globale sotto vincolo di budget condiviso.
    Non applica modifiche: restituisce solo una proposta verificata.
    """
    if target_deployment not in TARGET_DEPLOYMENTS:
        return _error_response(f"Accesso negato. Deployment '{target_deployment}' non autorizzato.")

    floor = DEPLOYMENT_MIN_REPLICAS[target_deployment]
    if desired_replicas < floor or desired_replicas > EMERGENCY_MAX_REPLICAS:
        return _error_response(
            f"Azione bloccata dalla Safety Policy: desired_replicas={desired_replicas} è fuori limiti "
            f"(Min servizio: {floor}, Max emergenza: {EMERGENCY_MAX_REPLICAS})."
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
                f"Piano non fattibile nel budget globale: {plan.get('reason', 'vincolo non soddisfatto')}"
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
        return _success_response(data, msg="Piano budget globale calcolato con successo.")
    except client.exceptions.ApiException as e:
        return _error_response(f"Errore K8s API ({e.status}) durante pianificazione budget: {e.reason}")
    except Exception as e:
        return _error_response(f"Errore interno durante pianificazione budget: {str(e)}")

@mcp.tool()
def execute_budget_allocation(
    target_deployment: str,
    desired_replicas: int,
    donor_priority_csv: str = "",
) -> dict[str, Any]:
    """
    Esegue una riallocazione di repliche con budget globale condiviso.
    Ordine sicuro: prima scale-down dei donor, poi scale-up del target.
    Se una step fallisce, tenta rollback delle modifiche gia applicate.
    """
    if target_deployment not in TARGET_DEPLOYMENTS:
        return _error_response(f"Accesso negato. Deployment '{target_deployment}' non autorizzato.")

    floor = DEPLOYMENT_MIN_REPLICAS[target_deployment]
    if desired_replicas < floor or desired_replicas > EMERGENCY_MAX_REPLICAS:
        return _error_response(
            f"Azione bloccata dalla Safety Policy: desired_replicas={desired_replicas} e fuori limiti "
            f"(Min servizio: {floor}, Max emergenza: {EMERGENCY_MAX_REPLICAS})."
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
                f"Esecuzione budget non fattibile: {plan.get('reason', 'vincolo non soddisfatto')}"
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
                    "Esecuzione parziale fallita: rollback tentato.",
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
        return _success_response(data, msg="Riallocazione budget eseguita con successo.")
    except client.exceptions.ApiException as e:
        return _error_response(f"Errore K8s API ({e.status}) durante esecuzione budget: {e.reason}")
    except Exception as e:
        return _error_response(f"Errore interno durante esecuzione budget: {str(e)}")

@mcp.tool()
def set_hpa_max_replicas(deployment: str, max_replicas: int) -> dict[str, Any]:
    """
    Imposta temporaneamente il limite massimo dell'HPA del deployment.
    Consentito solo nell'intervallo [BUDGET_MAX_REPLICAS, EMERGENCY_MAX_REPLICAS].
    """
    if deployment not in TARGET_DEPLOYMENTS:
        return _error_response(f"Accesso negato. Deployment '{deployment}' non autorizzato.")

    # Do not allow raising HPA max above per-service budget. Emergency increases must use
    # an explicit, audited path (e.g., operator intervention). This prevents the HPA
    # from autonomously scaling a deployment beyond the global shared budget.
    if max_replicas < BUDGET_MAX_REPLICAS or max_replicas > EMERGENCY_MAX_REPLICAS:
        return _error_response(
            f"Azione bloccata dalla Safety Policy: max_replicas={max_replicas} è fuori dai limiti consentiti "
            f"(Budget: {BUDGET_MAX_REPLICAS}, Max emergenza: {EMERGENCY_MAX_REPLICAS})."
        )

    if max_replicas > BUDGET_MAX_REPLICAS:
        return _error_response(
            f"Azione bloccata: non è consentito alzare l'HPA above il limite per-servizio del budget ({BUDGET_MAX_REPLICAS})."
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
                f"Azione bloccata: max_replicas={max_replicas} è inferiore a minReplicas corrente ({current_min})."
            )

        body = {"spec": {"maxReplicas": max_replicas}}
        autoscaling_api.patch_namespaced_horizontal_pod_autoscaler(
            name=deployment,
            namespace=NAMESPACE,
            body=body,
        )

        LOGGER.info(
            "Aggiornato limite HPA",
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
            msg=f"HPA maxReplicas aggiornato con successo a {max_replicas}.",
        )
    except client.exceptions.ApiException as e:
        return _error_response(f"Errore K8s API ({e.status}) durante aggiornamento HPA: {e.reason}")
    except Exception as e:
        return _error_response(f"Errore interno durante aggiornamento HPA: {str(e)}")

@mcp.tool()
def get_deployment_resources(deployment: str, namespace: str = "default") -> str:
    """Restituisce i limiti e le richieste di CPU e RAM attuali per un deployment."""
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
        report = [f"Tipo: {w_type}"]
        for c in containers:
            resources = c.resources
            limits = resources.limits if resources.limits else "Non impostati"
            requests = resources.requests if resources.requests else "Non impostati"
            report.append(f"Container '{c.name}': Limits {limits}, Requests {requests}")
        return " | ".join(report)
    except ApiException as e:
        if e.status == 404:
            return f"Nessun Deployment o StatefulSet trovato con il nome '{clean_name}'"
        return f"Errore nell'accesso alle API K8s: {e.reason} ({e.status})"
    except Exception as e:
        return f"Errore interno del tool: {str(e)}"

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
    """Cerca workload per nome parziale o esatto e restituisce lo stato di salute dei relativi Pod (fase, età, readiness, riavvii)."""
    query = (workload_query or "").strip().lower()
    
    try:
        core_api = client.CoreV1Api()
        apps_api = client.AppsV1Api()
        
        matched_workloads = []
        
        # 1. Recupera TUTTI i Deployment e StatefulSet
        deployments = apps_api.list_namespaced_deployment(namespace=NAMESPACE)
        for d in deployments.items:
            if query in d.metadata.name.lower():
                matched_workloads.append((d.metadata.name, "Deployment", d.spec.selector.match_labels))
                
        statefulsets = apps_api.list_namespaced_stateful_set(namespace=NAMESPACE)
        for s in statefulsets.items:
            if query in s.metadata.name.lower():
                matched_workloads.append((s.metadata.name, "StatefulSet", s.spec.selector.match_labels))

        if not matched_workloads:
            return f"Nessun workload trovato corrispondente alla query '{query}'."

        report = []
        now = datetime.datetime.now(datetime.timezone.utc)
        
        # 2. Per ogni match, interroga i Pod usando i label selector
        for name, w_type, labels_dict in matched_workloads:
            if not labels_dict:
                continue
                
            label_selector = ",".join([f"{k}={v}" for k, v in labels_dict.items()])
            pods = core_api.list_namespaced_pod(namespace=NAMESPACE, label_selector=label_selector)
            
            report.append(f"\n--- Stato per {w_type}: {name} ---")
            if not pods.items:
                report.append("Nessun Pod attivo trovato (possibile scale a 0).")
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
                    f"- Pod: {p_name} | Fase: {phase} | Età: {age_mins}m | "
                    f"Pronto: {ready_count}/{total_count} | Riavvii: {restarts} | {status_details.strip()}"
                )
                
        return "\n".join(report)

    except Exception as exc:
        return f"Errore API K8s durante la ricerca: {str(exc)}"

if __name__ == "__main__":
    mcp.run(transport="stdio")