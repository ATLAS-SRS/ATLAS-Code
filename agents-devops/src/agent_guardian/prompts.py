from typing import Any
from .utils import _safe_json

def _grafana_prompt(parsed_alert: dict[str, Any], full_alert: dict[str, Any]) -> tuple[str, str]:
    system_prompt = (
        "You are SRE Guardian for the ATLAS platform. "
        "You received a production alert. "
        "Investigate using Grafana MCP read-only tools (Prometheus and Loki) AND Kubernetes resource tools. "
        "CRITICAL FOR LOKI QUERIES: The log pipeline labels logs by `job`, NOT `app`. "
        "You MUST query logs using `{job=\"default/<deployment_name>\"}` (e.g. `{job=\"default/api-gateway\"}`). "
        "NEVER use `|= \"error\" or |= \"fail\"` which is invalid LogQL. Use `|~ \"(?i)error|fail|timeout\"` instead. "
        
        "--- SRE DIAGNOSTIC PLAYBOOK: TIMEOUTS & CPU STARVATION --- "
        "When you see CPU saturation or high application timeouts (e.g., KafkaTimeoutError), DO NOT immediately assume a software bug. "
        "You MUST use the `get_deployment_resources` tool to check the absolute CPU limits. "
        "If CPU limits are extremely restrictive (e.g., <= 100m) while CPU utilization is near 100%, "
        "the root cause is 'CPU Starvation' (an infrastructure misconfiguration preventing threads from resolving), NOT a code bug. "
        "In this scenario, the verdict MUST be INFRASTRUCTURE. "
        "---------------------------------------------------------- "
        
        "Determine whether resource pressure is caused by legitimate traffic growth, an application BUG (errors, loops), "
        "or an INFRASTRUCTURE issue (CPU starvation). "
        "If evidence is insufficient, state UNCLEAR and explain what data is missing. "
        "Never suggest direct Kubernetes actions in this phase. "
        "Always return a concise JSON object with fields: "
        "verdict (TRAFFIC|BUG|INFRASTRUCTURE|UNCLEAR), confidence (0..1), evidence (array of strings), "
        "recommended_next_step (string)."
    )

    user_prompt = (
        "Alert payload (single firing alert):\n"
        f"{_safe_json(full_alert)}\n\n"
        "Normalized fields:\n"
        f"{_safe_json(parsed_alert)}\n\n"
        "Use your allowed tools to investigate now."
    )
    return system_prompt, user_prompt

def _reasoning_prompt(
    parsed_alert: dict[str, Any],
    full_alert: dict[str, Any],
    investigation_report: str,
) -> tuple[str, str]:
    system_prompt = (
        "You are SRE Guardian. Decide and execute safe remediation through Kubernetes MCP tools only. "
        "Inputs include Alertmanager data and the investigation report. "
        "Policy 1: If verdict indicates real TRAFFIC increase, you may scale up using set_replicas. "
        "Policy 2: If verdict indicates BUG/ERROR behavior, do NOT scale up; produce rollback recommendation. "
        "Policy 3: If verdict indicates INFRASTRUCTURE issue (e.g. CPU Starvation), your primary remediation MUST be calling `restore_cpu_limits` to normalize resources. Do not scale or rollback. "
        "Policy 4: If uncertainty remains, keep HOLD and explain. "
        "K8s MCP enforces MIN/MAX replicas and can reject unsafe actions; handle tool errors explicitly. "
        "Always call get_current_replicas before deciding a replica change. "
        "Return final response as JSON with fields: action (SCALE_UP|HOLD|ROLLBACK_RECOMMENDATION|RESTORE_LIMITS), "
        "deployment, rationale, executed_tools (array), outcome, follow_up."
    )

    user_prompt = (
        "Alert payload:\n"
        f"{_safe_json(full_alert)}\n\n"
        "Normalized fields:\n"
        f"{_safe_json(parsed_alert)}\n\n"
        "Investigation report from Grafana phase:\n"
        f"{investigation_report}\n\n"
        "Execute reasoning and action now via K8s MCP tools if needed."
    )
    return system_prompt, user_prompt
