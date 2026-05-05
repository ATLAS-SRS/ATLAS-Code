import os
from pathlib import Path
import sys
from structured_logger import get_logger

LOGGER = get_logger("sre-guardian", stream=sys.stderr)
LOGGER.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5-coder").strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", "local-no-key").strip()
LLM_API_URL = (
    os.getenv("LLM_API_URL", "").strip()
    or os.getenv("LM_STUDIO_URL", "").strip()
    or "http://host.docker.internal:1234/v1"
)
MAX_TOOL_STEPS = int(os.getenv("MAX_TOOL_STEPS", "6"))
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "90"))

DEFAULT_AUTOSCALABLE_DEPLOYMENTS = "api-gateway,scoring-system,enrichment-system,notification-system"
AUTOSCALABLE_DEPLOYMENTS = {
    item.strip()
    for item in os.getenv("AUTOSCALABLE_DEPLOYMENTS", DEFAULT_AUTOSCALABLE_DEPLOYMENTS).split(",")
    if item.strip()
}

DEFAULT_MONITORED_WORKLOADS = (
    f"{DEFAULT_AUTOSCALABLE_DEPLOYMENTS},atlas-postgres-postgresql,atlas-redis-master"
)
MONITORED_WORKLOADS = {
    item.strip()
    for item in os.getenv("MONITORED_WORKLOADS", DEFAULT_MONITORED_WORKLOADS).split(",")
    if item.strip()
}

MONITORED_ONLY_WORKLOADS = {
    workload for workload in MONITORED_WORKLOADS if workload not in AUTOSCALABLE_DEPLOYMENTS
}

# Backwards-compatible alias for existing callers that still expect the old name.
TARGET_DEPLOYMENTS = AUTOSCALABLE_DEPLOYMENTS

def _normalize_llm_base_url(url: str) -> str:
    clean = url.rstrip("/")
    if not clean.endswith("/v1"):
        clean = f"{clean}/v1"
    return clean

def _resolve_k8s_mcp_script() -> str:
    override = os.getenv("K8S_MCP_SERVER_SCRIPT", "").strip()
    # Resolve relative to where it runs or where config config is
    # base_dir is agents-devops
    base_dir = Path(__file__).resolve().parent.parent.parent

    if override:
        candidate = Path(override)
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        if candidate.exists():
            return str(candidate)
        raise RuntimeError(f"Configured K8S_MCP_SERVER_SCRIPT not found: {candidate}")

    primary = base_dir / "mcp_server.py"
    if primary.exists():
        return str(primary)

    fallback = base_dir / "k8s_mcp.py"
    if fallback.exists():
        LOGGER.info(
            "mcp_server.py not found, using k8s_mcp.py fallback",
            extra={"script": str(fallback)},
        )
        return str(fallback)

    raise RuntimeError("Neither mcp_server.py nor k8s_mcp.py was found in agents-devops/")
