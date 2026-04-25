#!/usr/bin/env python3
import os
import sys
from typing import Any
from kubernetes import client, config
from mcp.server.fastmcp import FastMCP
from structured_logger import get_logger

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
BUDGET_MAX_REPLICAS = int(os.getenv("BUDGET_MAX_REPLICAS", "3"))
EMERGENCY_MAX_REPLICAS = int(
    os.getenv("EMERGENCY_MAX_REPLICAS", os.getenv("MAX_REPLICAS", "5"))
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
def set_hpa_max_replicas(deployment: str, max_replicas: int) -> dict[str, Any]:
    """
    Imposta temporaneamente il limite massimo dell'HPA del deployment.
    Consentito solo nell'intervallo [BUDGET_MAX_REPLICAS, EMERGENCY_MAX_REPLICAS].
    """
    if deployment not in TARGET_DEPLOYMENTS:
        return _error_response(f"Accesso negato. Deployment '{deployment}' non autorizzato.")

    if max_replicas < BUDGET_MAX_REPLICAS or max_replicas > EMERGENCY_MAX_REPLICAS:
        return _error_response(
            f"Azione bloccata dalla Safety Policy: max_replicas={max_replicas} è fuori dai limiti consentiti "
            f"(Budget: {BUDGET_MAX_REPLICAS}, Max emergenza: {EMERGENCY_MAX_REPLICAS})."
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

if __name__ == "__main__":
    mcp.run(transport="stdio")