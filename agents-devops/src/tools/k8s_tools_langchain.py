import os
from dataclasses import dataclass, field
from typing import Any
from kubernetes import client, config
from langchain_core.tools import StructuredTool
from structured_logger import get_logger

LOGGER = get_logger("k8s-tools")

@dataclass
class K8sConfig:
    """Encapsulates all configuration required for K8s operations."""
    namespace: str
    min_replicas: int
    max_replicas: int
    allowed_deployments: set[str] = field(default_factory=set)

    @classmethod
    def from_env(cls) -> "K8sConfig":
        """Factory to build config from environment variables."""
        deployments = {
            d.strip() for d in os.getenv(
                "TARGET_DEPLOYMENTS", 
                "api-gateway,scoring-system,enrichment-system,notification-system"
            ).split(",") if d.strip()
        }
        return cls(
            namespace=os.getenv("NAMESPACE", "default").strip(),
            min_replicas=int(os.getenv("MIN_REPLICAS", "1")),
            max_replicas=int(os.getenv("MAX_REPLICAS", "5")),
            allowed_deployments=deployments
        )

class KubernetesToolManager:
    """Manages Kubernetes state and provides LangChain-compatible tools."""
    
    def __init__(self, k8s_config: K8sConfig, apps_api: client.AppsV1Api = None):
        self.config = k8s_config
        
        # Allow dependency injection of the API client for easy unit testing.
        # If not provided, initialize it normally.
        if apps_api:
            self.apps_api = apps_api
        else:
            self.apps_api = self._initialize_k8s_client()

    def _initialize_k8s_client(self) -> client.AppsV1Api:
        """Bootstraps the K8s client."""
        try:
            config.load_incluster_config()
            LOGGER.info("Loaded in-cluster Kubernetes config")
        except Exception as e:
            LOGGER.warning(f"Failed to load in-cluster config, falling back to local: {e}")
            config.load_kube_config()
        return client.AppsV1Api()

    # --- Core Logic ---

    def get_current_replicas(self, deployment: str) -> dict[str, Any]:
        """Return the current replica count for a Kubernetes deployment."""
        if deployment not in self.config.allowed_deployments:
            return {"status": "error", "message": f"Deployment '{deployment}' non consentito."}

        try:
            scale_obj = self.apps_api.read_namespaced_deployment_scale(
                name=deployment,
                namespace=self.config.namespace,
            )
            return {"status": "success", "data": int(scale_obj.status.replicas or 0)}
        except client.exceptions.ApiException as api_exc:
            return {"status": "error", "message": f"K8s API error: {api_exc.reason}"}

    def set_replicas(self, deployment: str, replicas: int) -> dict[str, Any]:
        """Set the replica count of a Kubernetes deployment within guardrails."""
        if deployment not in self.config.allowed_deployments:
            return {"status": "error", "message": f"Deployment '{deployment}' non consentito."}
            
        if replicas < self.config.min_replicas or replicas > self.config.max_replicas:
            return {
                "status": "error", 
                "message": f"Replicas out of bounds ({self.config.min_replicas}-{self.config.max_replicas})."
            }

        try:
            body = {"spec": {"replicas": replicas}}
            self.apps_api.patch_namespaced_deployment_scale(
                name=deployment,
                namespace=self.config.namespace,
                body=body,
            )
            LOGGER.info(f"Scaled {deployment} to {replicas}")
            return {"status": "success", "message": f"Repliche impostate a {replicas}"}
        except client.exceptions.ApiException as api_exc:
            return {"status": "error", "message": f"K8s API error: {api_exc.reason}"}

    # --- LangChain Integration ---

    def get_tools(self) -> list[StructuredTool]:
        """Exposes the internal methods as LangChain StructuredTools."""
        return [
            StructuredTool.from_function(
                func=self.get_current_replicas,
                name="get_current_replicas",
                description="Returns the current replica count for a given Kubernetes deployment."
            ),
            StructuredTool.from_function(
                func=self.set_replicas,
                name="set_replicas",
                description="Sets the replica count for a Kubernetes deployment. Requires 'deployment' and 'replicas' arguments."
            )
        ]