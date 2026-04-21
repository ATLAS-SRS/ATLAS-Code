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

if __name__ == "__main__":
    mcp.run(transport="stdio")