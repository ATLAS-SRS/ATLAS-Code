import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests
from kubernetes import client, config
from mcp.server.fastmcp import FastMCP
from openai import OpenAI
from structured_logger import get_logger


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
NAMESPACE = os.getenv("NAMESPACE", "default")
TARGET_DEPLOYMENTS = os.getenv(
    "TARGET_DEPLOYMENTS",
    "api-gateway,scoring-system,enrichment-system,notification-system",
)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "15"))
PROMQL_RPS_QUERY = os.getenv(
    "PROMQL_RPS_QUERY",
    'sum(rate(http_requests_total{job="api-gateway"}[1m]))',
)
MIN_REPLICAS = int(os.getenv("MIN_REPLICAS", "1"))
MAX_REPLICAS = int(os.getenv("MAX_REPLICAS", "5"))
AGENT_IDLE_RPS_HINT = float(os.getenv("AGENT_IDLE_RPS_HINT", "0.05"))
RPS_REPLICA_THRESHOLDS = os.getenv("RPS_REPLICA_THRESHOLDS", "5,15,30,60")

LLM_API_URL = os.getenv("LLM_API_URL", "http://host.docker.internal:1234/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen/qwen2.5-coder-14b")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "20"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0"))
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
MAX_TOOL_STEPS = int(os.getenv("MAX_TOOL_STEPS", "8"))

PROMQL_RPS_FALLBACK_QUERIES = [
    'sum(rate(http_requests_total{app_kubernetes_io_name="api-gateway"}[1m]))',
]

SYSTEM_PROMPT = (
    "Sei un autoscaler autonomo di Kubernetes. Il tuo obiettivo è mantenere il sistema "
    "stabile. Hai a disposizione strumenti per leggere il traffico globale (RPS), leggere "
    "le repliche attuali di un deployment e modificare le repliche. Regole: Min repliche "
    "= 1, Max = 5. Workflow obbligatorio: prima richiama get_scaling_recommendation, poi "
    "confronta target_replicas con current_replicas e decidi in base al traffico reale. "
    "Se current_replicas > target_replicas devi scalare down (almeno di 1), se "
    "current_replicas < target_replicas devi scalare up (almeno di 1), altrimenti HOLD. "
    "Non mantenere 5 repliche senza una giustificazione numerica coerente con il traffico."
)

LOGGER = get_logger("scaling-agent")
LOGGER.setLevel(LOG_LEVEL)


def log_event(level: str, message: str, **fields: object) -> None:
    log_fn = getattr(LOGGER, level.lower(), LOGGER.info)
    log_fn(message, extra=fields)


@dataclass
class RuntimeContext:
    """Shared runtime dependencies for MCP tools executed by the local agent."""

    apps_api: client.AppsV1Api
    namespace: str
    prometheus_url: str


RUNTIME_CONTEXT: RuntimeContext | None = None
ALLOWED_DEPLOYMENTS: set[str] = set()

mcp = FastMCP("atlas-scaling-agent")


def _get_runtime_context() -> RuntimeContext:
    if RUNTIME_CONTEXT is None:
        raise RuntimeError("Runtime context is not initialized")
    return RUNTIME_CONTEXT


def _run_prometheus_query(prometheus_base_url: str, query: str) -> list[dict[str, Any]]:
    endpoint = f"{prometheus_base_url.rstrip('/')}/api/v1/query"
    response = requests.get(endpoint, params={"query": query}, timeout=10)
    response.raise_for_status()
    data = response.json()

    if data.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed for '{query}': {data}")

    result = data.get("data", {}).get("result", [])
    if not isinstance(result, list):
        raise RuntimeError(f"Invalid Prometheus result format for '{query}'")
    return result


def _normalize_openai_base_url(url: str) -> str:
    normalized = url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        normalized = normalized[: -len("/chat/completions")]
    if not normalized.endswith("/v1"):
        normalized = f"{normalized}/v1"
    return normalized


def _parse_rps_thresholds(raw_thresholds: str) -> list[float]:
    parts = [part.strip() for part in raw_thresholds.split(",") if part.strip()]
    if len(parts) != 4:
        raise RuntimeError(
            "RPS_REPLICA_THRESHOLDS must contain exactly 4 comma-separated numeric values"
        )

    thresholds = [float(value) for value in parts]
    if thresholds != sorted(thresholds):
        raise RuntimeError("RPS_REPLICA_THRESHOLDS must be sorted ascending")
    return thresholds


def _target_replicas_from_rps(rps: float, thresholds: list[float]) -> int:
    if rps <= thresholds[0]:
        return 1
    if rps <= thresholds[1]:
        return 2
    if rps <= thresholds[2]:
        return 3
    if rps <= thresholds[3]:
        return 4
    return 5


def _build_openai_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "get_scaling_recommendation",
                "description": (
                    "Calcola una raccomandazione operativa basata sul traffico attuale: "
                    "restituisce RPS osservato, repliche correnti, target repliche suggerito e "
                    "azione suggerita (SCALE_UP/SCALE_DOWN/HOLD). Questo e' lo strumento principale "
                    "per decidere scale up/down in base al carico."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "deployment": {
                            "type": "string",
                            "description": "Nome del deployment target.",
                        }
                    },
                    "required": ["deployment"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_rps",
                "description": (
                    "Legge il traffico globale in Requests Per Second dal Prometheus del cluster. "
                    "Usare questo strumento come primo passo per stimare il carico complessivo "
                    "prima di decidere una modifica delle repliche."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_current_replicas",
                "description": (
                    "Legge il numero di repliche correnti di un deployment Kubernetes nel namespace "
                    "gestito dall'agente. Da usare sempre prima di proporre uno scale up/down."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "deployment": {
                            "type": "string",
                            "description": "Nome del deployment da ispezionare.",
                        }
                    },
                    "required": ["deployment"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "set_replicas",
                "description": (
                    "Aggiorna il numero di repliche di un deployment Kubernetes. "
                    f"Vincoli hard: replicas deve essere compreso tra {MIN_REPLICAS} e {MAX_REPLICAS}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "deployment": {
                            "type": "string",
                            "description": "Nome del deployment da scalare.",
                        },
                        "replicas": {
                            "type": "integer",
                            "description": "Numero target di repliche desiderate.",
                        },
                    },
                    "required": ["deployment", "replicas"],
                },
            },
        },
    ]


@mcp.tool()
def get_scaling_recommendation(deployment: str) -> dict[str, Any]:
    """
    Calcola una raccomandazione di scaling basata sul traffico globale osservato.

    La funzione combina due misure operative correnti:
    - RPS globale (letto da Prometheus)
    - repliche correnti del deployment target

    In base alle soglie configurate in RPS_REPLICA_THRESHOLDS determina un target
    di repliche tra MIN_REPLICAS e MAX_REPLICAS e produce anche l'azione suggerita.

    Args:
        deployment: Nome del deployment da valutare.

    Returns:
        dict[str, Any]: Dizionario con rps, current_replicas, target_replicas,
        suggested_action e una spiegazione sintetica.

    Raises:
        ValueError: Se deployment non e' autorizzato.
        RuntimeError: Se le soglie RPS sono invalide.
    """

    if deployment not in ALLOWED_DEPLOYMENTS:
        raise ValueError(
            f"Deployment '{deployment}' non consentito. Consentiti: {sorted(ALLOWED_DEPLOYMENTS)}"
        )

    thresholds = _parse_rps_thresholds(RPS_REPLICA_THRESHOLDS)
    observed_rps = get_rps()
    current = get_current_replicas(deployment=deployment)
    target = _target_replicas_from_rps(observed_rps, thresholds)
    target = max(MIN_REPLICAS, min(MAX_REPLICAS, target))

    if current < target:
        suggested_action = "SCALE_UP"
    elif current > target:
        suggested_action = "SCALE_DOWN"
    else:
        suggested_action = "HOLD"

    recommendation = {
        "deployment": deployment,
        "rps": round(observed_rps, 6),
        "current_replicas": current,
        "target_replicas": target,
        "suggested_action": suggested_action,
        "thresholds": thresholds,
        "reason": "Target calcolato da soglie RPS configurate",
    }
    log_event("info", "Tool call completed", tool="get_scaling_recommendation", **recommendation)
    return recommendation


@mcp.tool()
def get_rps() -> float:
    """
    Legge il traffico globale in Requests Per Second (RPS) dall'infrastruttura Prometheus.

    Questo strumento esegue una query principale e, in caso di risultato vuoto,
    prova query di fallback predefinite. Il valore ritornato rappresenta il carico
    di ingresso complessivo del sistema ed e' utile per decisioni di autoscaling
    coordinate tra servizi multipli.

    Returns:
        float: RPS globale corrente osservato dal primo time series disponibile.

    Raises:
        RuntimeError: Se nessuna query produce un risultato valido.
        requests.HTTPError: Se Prometheus risponde con errore HTTP.
    """

    context = _get_runtime_context()
    queries = [PROMQL_RPS_QUERY, *PROMQL_RPS_FALLBACK_QUERIES]
    attempts: list[dict[str, str]] = []

    log_event("info", "Tool call started", tool="get_rps")
    for query in queries:
        result = _run_prometheus_query(context.prometheus_url, query)
        if not result:
            attempts.append({"query": query, "status": "empty_result"})
            continue

        value = result[0].get("value", [None, "0"])[1]
        rps = float(value)
        log_event(
            "info",
            "Tool call completed",
            tool="get_rps",
            query=query,
            rps=round(rps, 6),
        )
        return rps

    log_event("error", "Tool call failed", tool="get_rps", attempts=attempts)
    # If no series is available for gateway-focused queries, treat load as idle.
    log_event(
        "warning",
        "No RPS series found for configured queries; defaulting to 0",
        tool="get_rps",
        attempts=attempts,
    )
    return 0.0


@mcp.tool()
def get_current_replicas(deployment: str) -> int:
    """
    Restituisce il numero di repliche correnti di un deployment Kubernetes.

    Args:
        deployment: Nome del deployment target su cui leggere lo stato corrente.

    Returns:
        int: Numero di repliche correnti rilevate nel campo status del deployment scale.

    Raises:
        ValueError: Se il deployment non e' consentito dalla policy TARGET_DEPLOYMENTS.
        kubernetes.client.exceptions.ApiException: Se la chiamata Kubernetes API fallisce.
    """

    if deployment not in ALLOWED_DEPLOYMENTS:
        raise ValueError(
            f"Deployment '{deployment}' non consentito. Consentiti: {sorted(ALLOWED_DEPLOYMENTS)}"
        )

    context = _get_runtime_context()
    log_event("info", "Tool call started", tool="get_current_replicas", deployment=deployment)
    scale_obj = context.apps_api.read_namespaced_deployment_scale(
        name=deployment,
        namespace=context.namespace,
    )
    replicas = int(scale_obj.status.replicas or 0)
    log_event(
        "info",
        "Tool call completed",
        tool="get_current_replicas",
        deployment=deployment,
        replicas=replicas,
    )
    return replicas


@mcp.tool()
def set_replicas(deployment: str, replicas: int) -> str:
    """
    Applica un nuovo numero di repliche a un deployment Kubernetes.

    Args:
        deployment: Nome del deployment target da scalare.
        replicas: Numero desiderato di repliche da impostare.

    Returns:
        str: Messaggio confermato e leggibile che descrive l'azione effettuata.

    Raises:
        ValueError: Se il deployment non e' consentito o se replicas viola i guardrail.
        kubernetes.client.exceptions.ApiException: Se il patch della scala fallisce.
    """

    if deployment not in ALLOWED_DEPLOYMENTS:
        raise ValueError(
            f"Deployment '{deployment}' non consentito. Consentiti: {sorted(ALLOWED_DEPLOYMENTS)}"
        )
    if replicas < MIN_REPLICAS or replicas > MAX_REPLICAS:
        raise ValueError(
            f"replicas={replicas} fuori limite. Limiti: min={MIN_REPLICAS}, max={MAX_REPLICAS}"
        )

    context = _get_runtime_context()
    body = {"spec": {"replicas": replicas}}

    log_event(
        "info",
        "Tool call started",
        tool="set_replicas",
        deployment=deployment,
        requested_replicas=replicas,
    )
    context.apps_api.patch_namespaced_deployment_scale(
        name=deployment,
        namespace=context.namespace,
        body=body,
    )

    result_message = f"Repliche impostate a {replicas} per il deployment {deployment}"
    log_event(
        "info",
        "Tool call completed",
        tool="set_replicas",
        deployment=deployment,
        replicas=replicas,
    )
    return result_message


def _build_tool_dispatch() -> dict[str, Callable[..., Any]]:
    return {
        "get_scaling_recommendation": get_scaling_recommendation,
        "get_rps": get_rps,
        "get_current_replicas": get_current_replicas,
        "set_replicas": set_replicas,
    }


def _build_openai_client() -> OpenAI:
    base_url = _normalize_openai_base_url(LLM_API_URL)
    api_key = LLM_API_KEY or "local"
    log_event("info", "OpenAI client initialized", base_url=base_url, model=LLM_MODEL)
    return OpenAI(base_url=base_url, api_key=api_key, timeout=LLM_TIMEOUT)


def _run_deployment_agent_cycle(
    llm_client: OpenAI,
    deployment: str,
    observed_rps: float,
    observed_replicas: int,
    openai_tools: list[dict[str, Any]],
    tool_dispatch: dict[str, Callable[..., Any]],
    reconsideration_note: str | None = None,
) -> str:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Analizza il deployment seguente e decidi autonomamente se intervenire. "
                "Usa i tool quando necessario e, se modifichi lo stato, usa set_replicas. "
                "Al termine rispondi con un breve report operativo in JSON con campi: "
                "deployment, action, reason, final_replicas. "
                f"Deployment: {deployment}. Namespace: {NAMESPACE}. "
                f"Guardrail runtime: min_replicas={MIN_REPLICAS}, max_replicas={MAX_REPLICAS}. "
                f"Osservazioni iniziali: rps={round(observed_rps, 6)}, current_replicas={observed_replicas}. "
                "Se rps e' circa zero e current_replicas > min_replicas, preferisci scale down prudente."
                + (
                    f" Nota di riesame operativo: {reconsideration_note}"
                    if reconsideration_note
                    else ""
                )
            ),
        },
    ]

    for step in range(1, MAX_TOOL_STEPS + 1):
        response = llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            tools=openai_tools,
            tool_choice="auto",
            temperature=LLM_TEMPERATURE,
        )
        assistant = response.choices[0].message
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": assistant.content or "",
        }

        tool_calls = assistant.tool_calls or []
        if tool_calls:
            assistant_msg["tool_calls"] = [tool_call.model_dump() for tool_call in tool_calls]
        messages.append(assistant_msg)

        if not tool_calls:
            final_text = (assistant.content or "").strip()
            log_event(
                "info",
                "Agent cycle completed",
                deployment=deployment,
                tool_steps=step,
                assistant_message=final_text,
            )
            return final_text

        for tool_call in tool_calls:
            tool_name = tool_call.function.name
            raw_args = tool_call.function.arguments or "{}"
            tool_args = json.loads(raw_args)

            if tool_name not in tool_dispatch:
                tool_output = {
                    "ok": False,
                    "error": f"Tool sconosciuto: {tool_name}",
                }
            else:
                try:
                    log_event(
                        "info",
                        "Executing tool",
                        deployment=deployment,
                        tool=tool_name,
                        arguments=tool_args,
                    )
                    result = tool_dispatch[tool_name](**tool_args)
                    tool_output = {"ok": True, "result": result}
                except Exception as exc:
                    log_event(
                        "error",
                        "Tool execution error",
                        deployment=deployment,
                        tool=tool_name,
                        arguments=tool_args,
                        error=str(exc),
                    )
                    tool_output = {"ok": False, "error": str(exc)}

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(tool_output, ensure_ascii=True),
                }
            )

    timeout_report = (
        f"{{\"deployment\": \"{deployment}\", \"action\": \"HOLD\", "
        f"\"reason\": \"Max tool steps reached ({MAX_TOOL_STEPS})\", "
        "\"final_replicas\": null}}"
    )
    log_event(
        "warning",
        "Agent cycle reached max steps",
        deployment=deployment,
        max_tool_steps=MAX_TOOL_STEPS,
    )
    return timeout_report


def _load_kubernetes_config() -> None:
    try:
        config.load_incluster_config()
        log_event("info", "Loaded in-cluster Kubernetes config")
    except Exception:
        config.load_kube_config()
        log_event("warning", "Loaded local kube config as fallback")


def run() -> None:
    _load_kubernetes_config()
    apps_api = client.AppsV1Api()

    deployments = [item.strip() for item in TARGET_DEPLOYMENTS.split(",") if item.strip()]
    if not deployments:
        raise RuntimeError("TARGET_DEPLOYMENTS is empty after parsing")

    global RUNTIME_CONTEXT
    global ALLOWED_DEPLOYMENTS
    RUNTIME_CONTEXT = RuntimeContext(
        apps_api=apps_api,
        namespace=NAMESPACE,
        prometheus_url=PROMETHEUS_URL,
    )
    ALLOWED_DEPLOYMENTS = set(deployments)

    llm_client = _build_openai_client()
    openai_tools = _build_openai_tools()
    tool_dispatch = _build_tool_dispatch()

    log_event(
        "info",
        "MCP autonomous scaling agent started",
        namespace=NAMESPACE,
        target_deployments=deployments,
        check_interval=CHECK_INTERVAL,
        promql_rps_query=PROMQL_RPS_QUERY,
        llm_api_url=_normalize_openai_base_url(LLM_API_URL),
        llm_model=LLM_MODEL,
        min_replicas=MIN_REPLICAS,
        max_replicas=MAX_REPLICAS,
    )

    while True:
        for deployment in deployments:
            try:
                observed_rps = get_rps()
                observed_replicas = get_current_replicas(deployment=deployment)

                report = _run_deployment_agent_cycle(
                    llm_client=llm_client,
                    deployment=deployment,
                    observed_rps=observed_rps,
                    observed_replicas=observed_replicas,
                    openai_tools=openai_tools,
                    tool_dispatch=tool_dispatch,
                )

                final_replicas = get_current_replicas(deployment=deployment)
                if observed_rps <= AGENT_IDLE_RPS_HINT and final_replicas >= MAX_REPLICAS:
                    log_event(
                        "warning",
                        "High replicas during low traffic detected; requesting agent reconsideration",
                        deployment=deployment,
                        observed_rps=round(observed_rps, 6),
                        idle_hint_threshold=AGENT_IDLE_RPS_HINT,
                        current_replicas=final_replicas,
                        max_replicas=MAX_REPLICAS,
                    )

                    report = _run_deployment_agent_cycle(
                        llm_client=llm_client,
                        deployment=deployment,
                        observed_rps=observed_rps,
                        observed_replicas=final_replicas,
                        openai_tools=openai_tools,
                        tool_dispatch=tool_dispatch,
                        reconsideration_note=(
                            "Il deployment e' rimasto a max_replicas con traffico basso. "
                            "Riesamina e giustifica esplicitamente HOLD oppure applica set_replicas "
                            "per ridurre in sicurezza."
                        ),
                    )

                log_event(
                    "info",
                    "Deployment cycle report",
                    deployment=deployment,
                    report=report,
                    observed_rps=round(observed_rps, 6),
                )
            except Exception as exc:
                log_event(
                    "error",
                    "Deployment cycle error",
                    deployment=deployment,
                    error=str(exc),
                )
            finally:
                time.sleep(2)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run()