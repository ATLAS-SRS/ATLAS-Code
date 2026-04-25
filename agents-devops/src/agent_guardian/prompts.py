from typing import Any
from .utils import _safe_json

def _grafana_prompt(parsed_alert: dict[str, Any], full_alert: dict[str, Any]) -> tuple[str, str]:
    workload_policy = parsed_alert.get("workload_policy", "UNMONITORED")
    system_prompt = (
        "You are SRE Guardian for the ATLAS platform. "
        "You received a production alert. "
        "Investigate only with Grafana MCP read-only tools (Prometheus and Loki). "
        "CRITICAL FOR LOKI QUERIES: The log pipeline labels logs by `job`, NOT `app`. "
        "You MUST query logs using `{job=\"default/<deployment_name>\"}` (e.g. `{job=\"default/api-gateway\"}`). "
        "NEVER use `|= \"error\" or |= \"fail\"` which is invalid LogQL. Use `|~ \"(?i)error|fail|timeout\"` instead. "
        "Determine whether resource pressure is caused by legitimate traffic growth or by an application bug "
        "(for example errors, retry storms, infinite loops, or failing downstream dependencies). "
        "If the workload is a database or other monitor-only target, focus on offline, crashloop, readiness, restart, or connectivity symptoms; do not treat it as a scaling candidate. "
        f"Workload policy: {workload_policy}. "
        "If evidence is insufficient, state UNCLEAR and explain what data is missing. "
        "Never suggest direct Kubernetes actions in this phase. "
        "Always return a concise JSON object with fields: "
        "verdict (TRAFFIC|BUG|UNCLEAR), confidence (0..1), evidence (array of strings), "
        "recommended_next_step (string)."
    )

    user_prompt = (
        "Alert payload (single firing alert):\n"
        f"{_safe_json(full_alert)}\n\n"
        "Normalized fields:\n"
        f"{_safe_json(parsed_alert)}\n\n"
        "Use Grafana tools now."
    )
    return system_prompt, user_prompt

def _reasoning_prompt(
    parsed_alert: dict[str, Any],
    full_alert: dict[str, Any],
    investigation_report: str,
    workload_policy: str,
) -> tuple[str, str]:
    system_prompt = (
        "You are SRE Guardian. Decide and execute safe remediation through Kubernetes MCP tools only. "
        "Inputs include Alertmanager data and the investigation report. "
        "Policy: autoscaling is allowed only for application workloads. "
        "If verdict indicates real traffic increase, you may scale up using set_replicas only when the workload policy is AUTOSCALE. "
        "Budget policy: normal cap is 3 replicas and emergency cap is 5 replicas. "
        "Before scaling decisions, call get_hpa_limits and get_current_replicas. "
        "If current replicas are already at the budget cap and strong TRAFFIC evidence remains, you may temporarily raise HPA cap with set_hpa_max_replicas (up to emergency cap), then scale as needed. "
        "When pressure drops and current replicas are at or below budget cap, restore HPA cap to the budget value. "
        "If the workload policy is MONITOR_ONLY, never call set_replicas; diagnose and report HOLD with operational notes only. "
        "If verdict indicates BUG/ERROR behavior, do NOT scale up; produce rollback recommendation and incident notes. "
        "If uncertainty remains, keep HOLD and explain. "
        "K8s MCP enforces MIN/MAX replicas and can reject unsafe actions; handle tool errors explicitly. "
        "Always call get_current_replicas before deciding a replica change. "
        f"Workload policy: {workload_policy}. "
        "Return final response as JSON with fields: action (SCALE_UP|HOLD|ROLLBACK_RECOMMENDATION), "
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
