import json
import logging
import os
import time
from typing import Any, Dict, Tuple

import requests
from kubernetes import client, config


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
NAMESPACE = os.getenv("NAMESPACE", "default")
TARGET_DEPLOYMENT = os.getenv("TARGET_DEPLOYMENT", "api-gateway")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "15"))
PROMQL_RPS_QUERY = os.getenv(
    "PROMQL_RPS_QUERY",
    f'sum(rate(http_requests_total{{job="{TARGET_DEPLOYMENT}"}}[1m]))',
)

# Thresholds are provided to the LLM as policy hints.
SCALE_UP_THRESHOLD = float(os.getenv("SCALE_UP_THRESHOLD", "50"))
SCALE_DOWN_THRESHOLD = float(os.getenv("SCALE_DOWN_THRESHOLD", "10"))

MIN_REPLICAS = int(os.getenv("MIN_REPLICAS", "1"))
MAX_REPLICAS = int(os.getenv("MAX_REPLICAS", "5"))

LLM_API_URL = os.getenv(
    "LLM_API_URL",
    "http://host.docker.internal:1234/v1/chat/completions",
)
LLM_MODEL = os.getenv("LLM_MODEL", "qwen/qwen2.5-coder-14b")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "20"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0"))
LLM_API_KEY = os.getenv("LLM_API_KEY", "")

PROMQL_RPS_FALLBACK_QUERIES = [
    f'sum(rate(http_requests_total{{app_kubernetes_io_name="{TARGET_DEPLOYMENT}"}}[1m]))',
    f'sum(rate(http_requests_total{{namespace="{NAMESPACE}"}}[1m]))',
    "sum(rate(http_requests_total[1m]))",
]

VALID_ACTIONS = {"SCALE_UP", "SCALE_DOWN", "HOLD"}


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("scaling-agent")
    logger.setLevel(LOG_LEVEL)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


LOGGER = setup_logger()


def log_event(level: int, message: str, **fields: object) -> None:
    payload = {"message": message, **fields}
    LOGGER.log(level, json.dumps(payload, sort_keys=True))


def run_prometheus_query(prometheus_base_url: str, query: str) -> list:
    endpoint = f"{prometheus_base_url.rstrip('/')}/api/v1/query"
    response = requests.get(
        endpoint,
        params={"query": query},
        timeout=10,
    )
    response.raise_for_status()

    data = response.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed for '{query}': {data}")

    return data.get("data", {}).get("result", [])


def get_rps(prometheus_base_url: str) -> float:
    queries = [PROMQL_RPS_QUERY, *PROMQL_RPS_FALLBACK_QUERIES]
    attempts = []

    for query in queries:
        result = run_prometheus_query(prometheus_base_url, query)
        if not result:
            attempts.append({"query": query, "status": "empty_result"})
            continue

        value = result[0].get("value", [None, "0"])[1]
        rps = float(value)
        log_event(logging.INFO, "RPS query selected", query=query, rps=round(rps, 6))
        return rps

    raise RuntimeError(f"No RPS series found. Attempts: {attempts}")


def extract_json_object(text: str) -> Dict[str, Any]:
    stripped = text.strip()

    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"LLM output is not JSON: {text}")

    candidate = stripped[start : end + 1]
    parsed = json.loads(candidate)
    if not isinstance(parsed, dict):
        raise ValueError(f"LLM JSON is not an object: {parsed}")
    return parsed


def decide_scaling_action_with_llm(rps: float, current_replicas: int) -> Tuple[str, str]:
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"

    if current_replicas <= MIN_REPLICAS:
        allowed_actions = ["SCALE_UP", "HOLD"]
    elif current_replicas >= MAX_REPLICAS:
        allowed_actions = ["SCALE_DOWN", "HOLD"]
    else:
        allowed_actions = ["SCALE_UP", "SCALE_DOWN", "HOLD"]

    system_prompt = (
        "You are an autoscaling decision engine. "
        "Return ONLY valid JSON with fields: action and reason. "
        "Allowed action values are provided in the user payload under allowed_actions. "
        "You must pick ONLY one of those allowed_actions. "
        "Hard rule: never propose scaling below min_replicas or above max_replicas. "
        "Never return markdown, code fences, or extra text."
    )

    user_payload = {
        "target_deployment": TARGET_DEPLOYMENT,
        "namespace": NAMESPACE,
        "rps": round(rps, 6),
        "current_replicas": current_replicas,
        "min_replicas": MIN_REPLICAS,
        "max_replicas": MAX_REPLICAS,
        "policy_hints": {
            "scale_up_threshold": SCALE_UP_THRESHOLD,
            "scale_down_threshold": SCALE_DOWN_THRESHOLD,
        },
        "allowed_actions": allowed_actions,
        "required_output_example": {
            "action": "HOLD",
            "reason": "RPS is stable and within target range.",
        },
    }

    body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, sort_keys=True)},
        ],
        "temperature": LLM_TEMPERATURE,
        "max_tokens": 120,
    }

    response = requests.post(
        LLM_API_URL,
        headers=headers,
        json=body,
        timeout=LLM_TIMEOUT,
    )
    response.raise_for_status()

    raw = response.json()
    content = (
        raw.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )
    if not content:
        raise RuntimeError(f"LLM returned empty content: {raw}")

    parsed = extract_json_object(content)
    action = str(parsed.get("action", "")).upper().strip()
    reason = str(parsed.get("reason", "No reason provided")).strip()

    if action not in VALID_ACTIONS:
        raise RuntimeError(f"Invalid LLM action '{action}' from payload: {parsed}")

    if action not in allowed_actions:
        adjusted_reason = (
            f"LLM suggested '{action}' but allowed actions are {allowed_actions}; "
            "forcing HOLD due to replica bounds."
        )
        return "HOLD", adjusted_reason

    return action, reason


def get_current_replicas(api: client.AppsV1Api, namespace: str, deployment: str) -> int:
    scale_obj = api.read_namespaced_deployment_scale(name=deployment, namespace=namespace)
    return int(scale_obj.status.replicas or 0)


def set_replicas(
    api: client.AppsV1Api,
    namespace: str,
    deployment: str,
    replicas: int,
) -> None:
    body = {"spec": {"replicas": replicas}}
    api.patch_namespaced_deployment_scale(
        name=deployment,
        namespace=namespace,
        body=body,
    )


def run() -> None:
    config.load_incluster_config()
    apps_api = client.AppsV1Api()

    log_event(
        logging.INFO,
        "Scaling agent started",
        namespace=NAMESPACE,
        target_deployment=TARGET_DEPLOYMENT,
        check_interval=CHECK_INTERVAL,
        promql_rps_query=PROMQL_RPS_QUERY,
        scale_up_threshold=SCALE_UP_THRESHOLD,
        scale_down_threshold=SCALE_DOWN_THRESHOLD,
        llm_api_url=LLM_API_URL,
        llm_model=LLM_MODEL,
        min_replicas=MIN_REPLICAS,
        max_replicas=MAX_REPLICAS,
    )

    while True:
        try:
            rps = get_rps(PROMETHEUS_URL)
            current_replicas = get_current_replicas(apps_api, NAMESPACE, TARGET_DEPLOYMENT)

            log_event(
                logging.INFO,
                "Observed metrics",
                rps=round(rps, 4),
                current_replicas=current_replicas,
            )

            action, reason = decide_scaling_action_with_llm(rps, current_replicas)

            log_event(
                logging.INFO,
                "LLM scaling decision",
                action=action,
                reason=reason,
                rps=round(rps, 4),
                current_replicas=current_replicas,
            )

            desired_replicas = current_replicas

            if action == "SCALE_UP" and current_replicas < MAX_REPLICAS:
                desired_replicas = current_replicas + 1
                set_replicas(apps_api, NAMESPACE, TARGET_DEPLOYMENT, desired_replicas)
                log_event(
                    logging.INFO,
                    "Scaling up",
                    rps=round(rps, 4),
                    llm_reason=reason,
                    from_replicas=current_replicas,
                    to_replicas=desired_replicas,
                )
            elif action == "SCALE_DOWN" and current_replicas > MIN_REPLICAS:
                desired_replicas = current_replicas - 1
                set_replicas(apps_api, NAMESPACE, TARGET_DEPLOYMENT, desired_replicas)
                log_event(
                    logging.INFO,
                    "Scaling down",
                    rps=round(rps, 4),
                    llm_reason=reason,
                    from_replicas=current_replicas,
                    to_replicas=desired_replicas,
                )
            elif action == "SCALE_UP" and current_replicas >= MAX_REPLICAS:
                log_event(
                    logging.INFO,
                    "No scaling action",
                    reason="LLM requested scale up but max replicas already reached",
                    rps=round(rps, 4),
                    replicas=current_replicas,
                )
            elif action == "SCALE_DOWN" and current_replicas <= MIN_REPLICAS:
                log_event(
                    logging.INFO,
                    "No scaling action",
                    reason="LLM requested scale down but min replicas already reached",
                    rps=round(rps, 4),
                    replicas=current_replicas,
                )
            else:
                log_event(
                    logging.INFO,
                    "No scaling action",
                    reason=reason,
                    rps=round(rps, 4),
                    replicas=current_replicas,
                )

        except Exception as exc:
            log_event(logging.ERROR, "Scaling loop error", error=str(exc))

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run()