#!/usr/bin/env python3
import os
import sys
from typing import Any
from kubernetes import client, config
from mcp.server.fastmcp import FastMCP
from structured_logger import get_logger
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
MAX_REPLICAS = int(os.getenv("MAX_REPLICAS", "5")) # Il limite economico del progetto

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
mcp = FastMCP("atlas-k8s-controller")

def _error_response(msg: str) -> dict[str, Any]:
    return {"status": "error", "message": msg, "data": None}

def _success_response(data: Any, msg: str = "OK") -> dict[str, Any]:
    return {"status": "success", "message": msg, "data": data}

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
    Fallisce automaticamente se viola i limiti di budget o sicurezza (MIN/MAX_REPLICAS).
    """
    if deployment not in TARGET_DEPLOYMENTS:
        return _error_response(f"Accesso negato. Deployment '{deployment}' non autorizzato.")
    
    # GUARDRAIL ECONOMICO E DI SICUREZZA
    if replicas < MIN_REPLICAS or replicas > MAX_REPLICAS:
        return _error_response(
            f"Azione bloccata dalla Safety Policy: {replicas} è fuori dai limiti consentiti "
            f"(Min: {MIN_REPLICAS}, Max: {MAX_REPLICAS}). Richiesta ignorata."
        )

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
def get_deployment_resources(deployment: str, namespace: str = "default") -> str:
    """Restituisce i limiti e le richieste di CPU e RAM attuali per un deployment."""
    try:
        # Se usi l'esecuzione in-cluster, config.load_incluster_config()
        # Se sei fuori, config.load_kube_config()
        apps_v1 = client.AppsV1Api()
        
        dep = apps_v1.read_namespaced_deployment(name=deployment, namespace=namespace)
        containers = dep.spec.template.spec.containers
        
        report = []
        for c in containers:
            resources = c.resources
            limits = resources.limits if resources.limits else "Non impostati"
            requests = resources.requests if resources.requests else "Non impostati"
            report.append(f"Container '{c.name}': Limits {limits}, Requests {requests}")
            
        return " | ".join(report)
        
    except ApiException as e:
        return f"Errore nell'accesso alle API K8s: {e.reason} ({e.status})"
    except Exception as e:
        return f"Errore interno del tool: {str(e)}"

@mcp.tool()
def restore_cpu_limits(deployment: str) -> dict[str, Any]:
    """Restore the CPU limits of a deployment to normal healthy values (requests: 50m, limits: 250m) to mitigate CPU starvation/throttling."""
    deployment = (deployment or "").strip()
    if not deployment:
        return _error_response("Parameter 'deployment' is required")

    try:
        k8s_apps_api, _ = _ensure_clients()
        dep = k8s_apps_api.read_namespaced_deployment(name=deployment, namespace=NAMESPACE)

        containers = dep.spec.template.spec.containers if dep.spec and dep.spec.template and dep.spec.template.spec else []
        if not containers:
            return _error_response("Deployment has no containers to patch", data={"deployment": deployment})

        # Impostiamo limiti di default sicuri per ripristinare il servizio
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

        k8s_apps_api.patch_namespaced_deployment(
            name=deployment,
            namespace=NAMESPACE,
            body=patch_body,
        )

        LOGGER.info(
            "Restored deployment CPU limits to healthy defaults",
            extra={
                "deployment": deployment,
                "namespace": NAMESPACE,
                "requests": safe_requests,
                "limits": safe_limits
            },
        )
        return _success_response(
            {
                "deployment": deployment,
                "status": "restored",
                "cpu_requests": safe_requests,
                "cpu_limits": safe_limits
            },
            message="CPU limits restored successfully"
        )
    except ApiException as exc:
        return _error_response(f"Kubernetes API error: {exc.reason}", data={"deployment": deployment})
    except Exception as exc:
        return _error_response(str(exc), data={"deployment": deployment})

if __name__ == "__main__":
    mcp.run(transport="stdio")