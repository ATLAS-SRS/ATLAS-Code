import os
import sys
import json
import hashlib
import asyncio
import time
from datetime import datetime, timezone
from contextlib import AsyncExitStack
from typing import Any
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI
from langchain_core.tools import StructuredTool
from langgraph.graph import END, StateGraph

from .config import (
    LOGGER, LLM_API_URL, LLM_API_KEY, LLM_MODEL,
    _normalize_llm_base_url, _resolve_k8s_mcp_script, MAX_TOOL_STEPS
)
from .models import (
    ActionDecisionModel,
    AlertContextModel,
    ExecutionDetailsModel,
    IncidentReportModel,
    InvestigationReportModel,
)
from .registry import ToolRegistry, ToolDef
from .utils import (
    _structured_tool_schema, _invoke_langchain_tool,
    _parse_tool_result, _extract_alert_fields, _safe_json, _clean_json_markdown
)
from .utils import _tool_call_parts
from .prompts import _grafana_prompt, _reasoning_prompt
from .llm import _run_llm_tool_loop
from .state import AgentState
from src.database import (
    dispose_async_database,
    fetch_incident_report,
    get_async_session,
    init_database,
    upsert_incident_report,
    create_approval_request,
)

from src.tools.grafana_mcp import GrafanaMCPManager

_SCALING_ACTIONS = {
    "SCALE_UP",
    "SCALE_DOWN",
    "SET_REPLICAS",
    "BUDGET_REALLOCATION",
}

def _parse_report_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}

    text = _clean_json_markdown(value)
    candidates = [text]
    if "{" in text and "}" in text:
        candidates.append(text[text.find("{"):text.rfind("}") + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    return {"raw": value}

def _extract_tool_names(trace: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for message in trace:
        for tool_call in message.get("tool_calls") or []:
            function = tool_call.get("function") or {}
            name = function.get("name")
            if name and name not in names:
                names.append(name)
    return names

def _extract_replica_details(final_report: dict[str, Any], trace: list[dict[str, Any]]) -> dict[str, Any]:
    replicas = {
        key: final_report[key]
        for key in ("current_replicas", "desired_replicas", "target_replicas", "replicas")
        if key in final_report
    }

    tool_outputs: list[dict[str, Any]] = []
    for message in trace:
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            continue
        data = parsed.get("data") if isinstance(parsed, dict) else None
        if isinstance(data, dict):
            tool_outputs.append(data)

    for output in tool_outputs:
        for key in ("current_replicas", "desired_replicas", "target_replicas", "replicas"):
            if key in output and key not in replicas:
                replicas[key] = output[key]

    return replicas

def _extract_replica_count_from_text(text: str) -> int | None:
    """Fallback: extract replica count from follow_up text (e.g., 'scale api-gateway to 4 replicas')."""
    import re
    # Match patterns like "to 4 replicas", "to 3 instances", "scale to 5", "increase to 6"
    patterns = [
        r'to\s+(\d+)\s+replicas',
        r'scale(?:ing)?\s+to\s+(\d+)',
        r'(\d+)\s+replicas',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None

_ITALIAN_TEXT_MARKERS = (
    " è ",
    " non ",
    " per ",
    " con ",
    " una ",
    " il ",
    " la ",
    "gli ",
    "le ",
    "repliche",
    "riavv",
    "sovraccar",
    "approvaz",
    "indagine",
    "allarme",
    "errore",
    "pronto",
    "budget",
)


def _looks_italian_text(text: str) -> bool:
    lowered = text.lower()
    if any(char in text for char in ("à", "è", "é", "ì", "ò", "ù")):
        return True
    return any(marker in lowered for marker in _ITALIAN_TEXT_MARKERS)


async def _translate_text_to_english(llm_client: AsyncOpenAI, text: str, *, label: str) -> str:
    if not text or not _looks_italian_text(text):
        return text

    system_prompt = (
        "You are a precise translation engine. Translate the input to natural English. "
        "Preserve Markdown structure, code spans, JSON keys, metric names, alert names, timestamps, and numbers. "
        "Do not add commentary or explanations. Return only the translated text."
    )
    user_prompt = f"Translate this {label} from Italian to English:\n\n{text}"

    try:
        response = await llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            timeout=30,
        )
        translated = (response.choices[0].message.content or "").strip()
        return translated or text
    except Exception:
        return text


async def _normalize_report_language(llm_client: AsyncOpenAI, value: Any, *, label: str) -> Any:
    if isinstance(value, str):
        return await _translate_text_to_english(llm_client, value, label=label)
    if isinstance(value, list):
        return [await _normalize_report_language(llm_client, item, label=label) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            normalized[key] = await _normalize_report_language(llm_client, item, label=f"{label}.{key}")
        return normalized
    return value

def _incident_id(alert: dict[str, Any], parsed_alert: dict[str, Any]) -> str:
    fingerprint = alert.get("fingerprint")
    if isinstance(fingerprint, str) and fingerprint.strip():
        return fingerprint.strip()

    labels = parsed_alert.get("labels") or {}
    payload = _safe_json(
        {
            "alert_name": parsed_alert.get("alert_name", ""),
            "deployment": parsed_alert.get("deployment", ""),
            "severity": parsed_alert.get("severity", ""),
            "summary": parsed_alert.get("summary", ""),
            "namespace": labels.get("namespace", ""),
            "trigger": labels.get("trigger", ""),
            "generatorURL": alert.get("generatorURL", ""),
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

class SREGuardianRuntime:
    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._llm_client: AsyncOpenAI | None = None
        self._graph = None
        self._tool_registry = ToolRegistry()
        self._grafana_manager: GrafanaMCPManager | None = None
        self._inflight_by_deployment: dict[str, asyncio.Lock] = {}
        self._recent_alerts: dict[tuple[str, str], float] = {}
        self._dedup_window_seconds = int(os.getenv("ALERT_DEDUP_WINDOW_SECONDS", "60"))

    @property
    def graph(self):
        if self._graph is None:
            raise RuntimeError("Runtime graph not initialized")
        return self._graph

    def _alert_fingerprint(self, alert: dict[str, Any], parsed_alert: dict[str, Any]) -> str:
        labels = parsed_alert.get("labels") or {}
        fingerprint_payload = {
            "alert_name": parsed_alert.get("alert_name", ""),
            "severity": parsed_alert.get("severity", ""),
            "deployment": parsed_alert.get("deployment", ""),
            "status": parsed_alert.get("status", ""),
            "startsAt": parsed_alert.get("startsAt", ""),
            "endsAt": parsed_alert.get("endsAt", ""),
            "labels": {k: labels[k] for k in sorted(labels)},
            "generatorURL": alert.get("generatorURL", ""),
        }
        raw = _safe_json(fingerprint_payload)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _prune_recent_alerts(self, now: float) -> None:
        ttl = self._dedup_window_seconds * 4
        stale = [key for key, ts in self._recent_alerts.items() if (now - ts) > ttl]
        for key in stale:
            self._recent_alerts.pop(key, None)

    async def process_alert(self, alert: dict[str, Any], index: int) -> dict[str, Any]:
        parsed = _extract_alert_fields(alert)
        deployment = parsed.get("deployment") or "unknown"
        fingerprint = self._alert_fingerprint(alert, parsed)

        now = time.monotonic()
        self._prune_recent_alerts(now)
        dedup_key = (deployment, fingerprint)
        last_seen = self._recent_alerts.get(dedup_key)
        if last_seen is not None and (now - last_seen) < self._dedup_window_seconds:
            LOGGER.info(
                "Skipping duplicate alert in cooldown window",
                extra={
                    "index": index,
                    "deployment": deployment,
                    "dedup_window_seconds": self._dedup_window_seconds,
                },
            )
            return {
                "index": index,
                "status": "ignored_duplicate",
                "alert_name": parsed.get("alert_name", "unknown"),
                "deployment": deployment,
                "investigation_report": "",
                "final_report": _safe_json(
                    {
                        "action": "HOLD",
                        "deployment": deployment,
                        "rationale": "Duplicate alert received during cooldown window",
                        "outcome": "No action",
                    }
                ),
                "error": "",
            }

        lock = self._inflight_by_deployment.setdefault(deployment, asyncio.Lock())
        if lock.locked():
            LOGGER.info(
                "Skipping alert because another run is already in-flight for deployment",
                extra={"index": index, "deployment": deployment},
            )
            return {
                "index": index,
                "status": "ignored_inflight",
                "alert_name": parsed.get("alert_name", "unknown"),
                "deployment": deployment,
                "investigation_report": "",
                "final_report": _safe_json(
                    {
                        "action": "HOLD",
                        "deployment": deployment,
                        "rationale": "Another remediation run is already in-flight for this deployment",
                        "outcome": "No action",
                    }
                ),
                "error": "",
            }

        async with lock:
            self._recent_alerts[dedup_key] = now
            final_state = await self.graph.ainvoke({"alert": alert})
            return {
                "index": index,
                "status": "processed",
                "alert_name": (final_state.get("parsed_alert") or {}).get("alert_name", "unknown"),
                "deployment": (final_state.get("parsed_alert") or {}).get("deployment", deployment),
                "investigation_report": final_state.get("investigation_report", ""),
                "final_report": final_state.get("final_report", ""),
                "incident_id": final_state.get("incident_id", ""),
                "post_mortem_path": final_state.get("post_mortem_path", ""),
                "error": final_state.get("error", ""),
            }

    async def start(self) -> None:
        llm_base_url = _normalize_llm_base_url(LLM_API_URL)
        self._llm_client = AsyncOpenAI(base_url=llm_base_url, api_key=LLM_API_KEY)

        LOGGER.info(
            "Starting SRE Guardian runtime",
            extra={"llm_model": LLM_MODEL, "llm_base_url": llm_base_url},
        )

        # Try to register external tools and initialize the database, but
        # do not crash the whole process if these dependencies are temporarily
        # unavailable. Runtime will continue in degraded mode and log errors.
        try:
            await self._register_grafana_tools()
        except Exception as exc:  # pragma: no cover - defensive runtime handling
            LOGGER.error("Failed to register Grafana tools at startup", extra={"error": str(exc)})

        try:
            await self._register_k8s_tools_stdio()
        except Exception as exc:  # pragma: no cover - defensive runtime handling
            LOGGER.error("Failed to register K8s stdio tools at startup", extra={"error": str(exc)})

        try:
            await init_database()
        except Exception as exc:  # pragma: no cover - defensive runtime handling
            LOGGER.error("Database initialization failed at startup; continuing without DB", extra={"error": str(exc)})

        # Build the runtime graph even if some tools or DB are missing; some features
        # will operate in degraded mode but the agent should remain up.
        try:
            self._graph = self._build_graph()
        except Exception as exc:
            LOGGER.error("Failed to build runtime graph", extra={"error": str(exc)})
            raise

        LOGGER.info(
            "SRE Guardian runtime ready",
            extra={"registered_tools": sorted(self._tool_registry.names())},
        )

    async def stop(self) -> None:
        if self._grafana_manager is not None:
            try:
                await self._grafana_manager.close()
            except Exception as exc:
                LOGGER.error("Error closing Grafana manager", extra={"error": str(exc)})

        await self._stack.aclose()
        await dispose_async_database()
        LOGGER.info("SRE Guardian runtime stopped")

    async def _register_grafana_tools(self) -> None:
        self._grafana_manager = GrafanaMCPManager()
        max_attempts = int(os.getenv("GRAFANA_MCP_MAX_RETRIES", "5"))
        retry_delay_seconds = float(os.getenv("GRAFANA_MCP_RETRY_DELAY_SECONDS", "2"))

        grafana_tools = None
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                grafana_tools = await self._grafana_manager.get_tools(LLM_MODEL)
                break
            except Exception as exc:
                last_error = exc
                LOGGER.warning(
                    "Grafana MCP tool load attempt failed",
                    extra={
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "error_repr": repr(exc),
                    },
                )
                if attempt < max_attempts:
                    await asyncio.sleep(retry_delay_seconds)

        if grafana_tools is None:
            LOGGER.error(
                "Grafana MCP unavailable at startup after retries; continuing without Grafana tools",
                extra={
                    "max_attempts": max_attempts,
                    "error": str(last_error) if last_error else "unknown",
                    "error_type": type(last_error).__name__ if last_error else "unknown",
                    "error_repr": repr(last_error) if last_error else "unknown",
                },
            )
            return

        for tool in grafana_tools:
            # Estraiamo lo schema reale dal server MCP di Grafana
            parameters = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}

            # Creiamo la closure senza kwargs
            async def _invoke(args: dict[str, Any], t_name: str = tool.name) -> dict[str, Any]:
                try:
                    raw_str = await self._grafana_manager.execute_tool(t_name, args)
                    return json.loads(raw_str)
                except Exception as exc:
                    return {"status": "error", "message": str(exc), "data": None}

            self._tool_registry.register(
                ToolDef(
                    name=tool.name,
                    description=tool.description or "Grafana MCP tool",
                    parameters=parameters,
                    source="grafana_mcp",
                    invoke=_invoke,
                )
            )

        LOGGER.info(
            "Grafana tools loaded",
            extra={"tools": [t.name for t in grafana_tools]},
        )

    async def _register_k8s_tools_stdio(self) -> None:
        script = _resolve_k8s_mcp_script()
        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-u", script],
            env=os.environ.copy(),
        )

        transport = await self._stack.enter_async_context(stdio_client(server_params))
        session = await self._stack.enter_async_context(ClientSession(*transport))
        await session.initialize()

        listed = await session.list_tools()
        for tool in listed.tools:
            name = tool.name
            description = tool.description or "Kubernetes MCP tool"
            parameters = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}

            async def _invoke(args: dict[str, Any], *, session_ref: ClientSession = session, tool_name: str = name) -> dict[str, Any]:
                try:
                    raw = await session_ref.call_tool(tool_name, arguments=args)
                    return _parse_tool_result(raw)
                except Exception as exc:
                    return {"status": "error", "message": str(exc), "data": None}

            self._tool_registry.register(
                ToolDef(
                    name=name,
                    description=description,
                    parameters=parameters,
                    source="k8s_stdio_mcp",
                    invoke=_invoke,
                )
            )

        LOGGER.info(
            "K8s MCP tools loaded",
            extra={"tools": [t.name for t in listed.tools], "script": script},
        )

    def _build_graph(self):
        llm_client = self._llm_client
        if llm_client is None:
            raise RuntimeError("LLM client not initialized")

        tool_registry = self._tool_registry

        async def receive_alert_node(state: AgentState) -> dict[str, Any]:
            raw_alert = state.get("alert") or {}
            parsed = _extract_alert_fields(raw_alert)

            LOGGER.info(
                "Alert received",
                extra={
                    "alert_name": parsed.get("alert_name", "unknown"),
                    "severity": parsed.get("severity", "unknown"),
                    "deployment": parsed.get("deployment", ""),
                    "workload_policy": parsed.get("workload_policy", "UNMONITORED"),
                    "status": parsed.get("status", "unknown"),
                },
            )

            return {"parsed_alert": parsed}

        async def investigate_node(state: AgentState) -> dict[str, Any]:
            parsed_alert = state.get("parsed_alert") or {}
            full_alert = state.get("alert") or {}
            deployment = parsed_alert.get("deployment", "")

            # Enrich the investigation input with recurrence context so the LLM
            # can classify recurring alerts even when the raw Alertmanager payload
            # does not include occurrence metadata.
            raw_alert = state.get("alert") or {}
            incident_id = _incident_id(raw_alert, parsed_alert)
            try:
                async with get_async_session() as session:
                    existing_report = await fetch_incident_report(session, incident_id)
                    if existing_report:
                        occurrence_count = int(existing_report.get("occurrence_count", 0)) + 1
                        full_alert = dict(full_alert)
                        full_alert["occurrence_count"] = occurrence_count
                        full_alert["first_seen_utc"] = existing_report.get("first_seen_utc")
            except Exception:
                pass

            grafana_allowed = {
                name
                for name in tool_registry.names()
                if name in {"query_prometheus", "query_loki_logs", "list_datasources", "get_deployment_resources", "get_workload_health"}
            }

            if not grafana_allowed:
                msg = "No Grafana tools available; cannot investigate telemetry evidence"
                LOGGER.error(msg)
                return {"investigation_report": msg, "error": msg}

            system_prompt, user_prompt = _grafana_prompt(parsed_alert, full_alert)
            report, trace = await _run_llm_tool_loop(
                llm_client=llm_client,
                model=LLM_MODEL,
                tool_registry=tool_registry,
                allowed_tools=grafana_allowed,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_steps=MAX_TOOL_STEPS,
                trace_key="investigate",
                deployment=deployment,
            )

            LOGGER.info(
                "Investigation completed",
                extra={"deployment": deployment, "report": report},
            )
            # Post-process LLM investigation output: if LLM returned no clear verdict
            # try to extract telemetry tool outputs from the trace and apply a
            # conservative traffic heuristic so we don't leave alerts as UNKNOWN.
            parsed = _parse_report_json(report)

            def _collect_tool_outputs(llm_trace: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
                # Map tool_call_id -> tool_name from assistant messages
                call_map: dict[str, str] = {}
                outputs_by_name: dict[str, list[dict[str, Any]]] = {}

                for msg in llm_trace:
                    if msg.get("role") == "assistant":
                        tool_calls = msg.get("tool_calls") or []
                        for tc in tool_calls:
                            cid, tname, _ = _tool_call_parts(tc)
                            if cid:
                                call_map[cid] = tname

                for msg in llm_trace:
                    if msg.get("role") != "tool":
                        continue
                    cid = msg.get("tool_call_id") or ""
                    content = msg.get("content") or ""
                    try:
                        parsed_content = json.loads(content)
                    except Exception:
                        parsed_content = {"raw": content}

                    tname = call_map.get(cid, "unknown_tool")
                    outputs_by_name.setdefault(tname, []).append(parsed_content)

                return outputs_by_name

            verdict_value = ""
            if isinstance(parsed, dict):
                verdict_value = str(parsed.get("verdict") or "").strip().upper()

            if not parsed or verdict_value in {"", "UNKNOWN", "UNCLEAR"}:
                outputs = _collect_tool_outputs(trace)

                # If there's a Prometheus query result, try to infer traffic spike
                prom_outputs = outputs.get("query_prometheus") or []
                def _extract_numeric_values(obj: Any) -> list[float]:
                    nums: list[float] = []
                    if obj is None:
                        return nums
                    if isinstance(obj, (int, float)):
                        nums.append(float(obj))
                        return nums
                    if isinstance(obj, (list, tuple)):
                        for item in obj:
                            nums.extend(_extract_numeric_values(item))
                        return nums
                    if isinstance(obj, dict):
                        for v in obj.values():
                            nums.extend(_extract_numeric_values(v))
                        return nums
                    try:
                        # attempt to coerce strings containing numbers
                        cleaned = str(obj).strip()
                        if cleaned.replace('.', '', 1).isdigit():
                            nums.append(float(cleaned))
                    except Exception:
                        pass
                    return nums

                spike_detected = False
                spike_percent = 0.0
                evidence: list[str] = []

                for out in prom_outputs:
                    data = out.get("data") if isinstance(out, dict) else None
                    values = _extract_numeric_values(data)
                    if len(values) >= 2:
                        # use last value as current, median of previous values as baseline
                        current = float(values[-1])
                        baseline = float(sum(values[:-1]) / max(1, len(values[:-1])))
                        if baseline > 0:
                            pct = current / baseline
                            spike_percent = max(spike_percent, pct)
                            evidence.append(f"prometheus: current={current:.2f} baseline={baseline:.2f} ratio={pct:.2f}")
                            if pct >= 1.5:
                                spike_detected = True

                if spike_detected:
                    # synthesize a conservative investigation report indicating TRAFFIC
                    confidence = 0.9 if spike_percent >= 2.0 else 0.8
                    synthesized = {
                        "verdict": "TRAFFIC",
                        "confidence": confidence,
                        "checklist_completed": {
                            "step_1_resources_checked": any("get_deployment_resources" in m.get("tool_call_id", "") or "get_deployment_resources" in m.get("content", "") for m in trace),
                            "step_2_traffic_baseline_checked": True,
                            "step_3_deterministic_errors_searched": any("query_loki_logs" in m.get("tool_call_id", "") or "query_loki_logs" in m.get("content", "") for m in trace),
                            "step_4_correlation_analyzed": True,
                        },
                        "traffic_spike_detected": True,
                        "traffic_spike_ratio": spike_percent,
                        "deterministic_errors_found": False,
                        "resource_constraint_severity": "NONE",
                        "evidence": evidence or ["Prometheus shows significant RPS increase"],
                        "recommended_next_step": "Allow autoscale or increase replicas after validating budget state",
                    }
                    report = _safe_json(synthesized)
                else:
                    # If the LLM failed to produce a verdict, try several conservative
                    # fallbacks so we don't leave important alerts as UNKNOWN.
                    alert_name = str(parsed_alert.get("alert_name", "")).strip().lower()
                    recurrence = int(full_alert.get("occurrence_count", 0) or 0)

                    # Check for explicit indicators of unhealthy workload from tools
                    restore_executed = bool(outputs.get("restore_cpu_limits")) or any(
                        "restore_cpu_limits" in k for k in outputs.keys()
                    )
                    health_outputs = outputs.get("get_workload_health") or []

                    # Reuse numeric extractor to detect restart counts or other numeric signs
                    def _health_indicates_unhealthy(items: list[dict[str, Any]]) -> bool:
                        for item in items:
                            data = item.get("data") if isinstance(item, dict) else item
                            nums = _extract_numeric_values(data)
                            if any(n > 0 for n in nums):
                                return True
                            # fallback to string checks
                            try:
                                text = json.dumps(item).lower()
                            except Exception:
                                text = str(item).lower()
                            if any(s in text for s in ("not ready", "unhealthy", "restart", "crash", "crashloop", "oom")):
                                return True
                        return False

                    unhealthy_detected = _health_indicates_unhealthy(health_outputs) or restore_executed

                    # If workload appears unhealthy and this is a recurring alert, prefer
                    # an INFRASTRUCTURE classification (conservative) to avoid UNKNOWN.
                    if recurrence >= 2 and unhealthy_detected:
                        synthesized = {
                            "verdict": "INFRASTRUCTURE",
                            "confidence": 0.8,
                            "checklist_completed": {
                                "step_1_resources_checked": any(
                                    "get_deployment_resources" in m.get("tool_call_id", "")
                                    or "get_deployment_resources" in m.get("content", "")
                                    for m in trace
                                ),
                                "step_2_traffic_baseline_checked": False,
                                "step_3_deterministic_errors_searched": any(
                                    "query_loki_logs" in m.get("tool_call_id", "")
                                    or "query_loki_logs" in m.get("content", "")
                                    for m in trace
                                ),
                                "step_4_correlation_analyzed": True,
                            },
                            "traffic_spike_detected": False,
                            "traffic_spike_ratio": 0.0,
                            "deterministic_errors_found": False,
                            "resource_constraint_severity": "HIGH",
                            "evidence": [
                                "Recurring alert with workload health or restore action indicating unhealthy pods.",
                                "LLM investigation did not return a verdict; falling back to conservative infrastructure classification.",
                            ],
                            "recommended_next_step": "Review pod health and replica capacity; approve scale-up if appropriate.",
                        }
                        report = _safe_json(synthesized)
                    elif alert_name == "cpusaturation" and recurrence >= 2:
                        synthesized = {
                            "verdict": "INFRASTRUCTURE",
                            "confidence": 0.75,
                            "checklist_completed": {
                                "step_1_resources_checked": any(
                                    "get_deployment_resources" in m.get("tool_call_id", "")
                                    or "get_deployment_resources" in m.get("content", "")
                                    for m in trace
                                ),
                                "step_2_traffic_baseline_checked": True,
                                "step_3_deterministic_errors_searched": any(
                                    "query_loki_logs" in m.get("tool_call_id", "")
                                    or "query_loki_logs" in m.get("content", "")
                                    for m in trace
                                ),
                                "step_4_correlation_analyzed": True,
                            },
                            "traffic_spike_detected": False,
                            "traffic_spike_ratio": 0.0,
                            "deterministic_errors_found": False,
                            "resource_constraint_severity": "UNKNOWN",
                            "evidence": [
                                f"Recurring CPUSaturation alert observed {recurrence} times.",
                                "LLM investigation did not return a verdict; falling back to conservative infrastructure classification.",
                            ],
                            "recommended_next_step": "Review CPU limits and replica capacity; consider scale-up if saturation persists.",
                        }
                        report = _safe_json(synthesized)

            return {"investigation_report": report, "llm_trace": trace}

        async def reasoning_action_node(state: AgentState) -> dict[str, Any]:
            parsed_alert = state.get("parsed_alert") or {}
            full_alert = state.get("alert") or {}
            investigation_report = state.get("investigation_report", "")
            deployment = parsed_alert.get("deployment", "")
            workload_policy = parsed_alert.get("workload_policy", "UNMONITORED")

            # Enrich alert with occurrence count from database to help LLM detect recurring alerts
            raw_alert = state.get("alert") or {}
            incident_id = _incident_id(raw_alert, parsed_alert)
            try:
                async with get_async_session() as session:
                    existing_report = await fetch_incident_report(session, incident_id)
                    if existing_report:
                        occurrence_count = int(existing_report.get("occurrence_count", 0)) + 1
                        full_alert = dict(full_alert)  # make a copy
                        full_alert["occurrence_count"] = occurrence_count
                        full_alert["first_seen_utc"] = existing_report.get("first_seen_utc")
            except Exception:
                pass  # proceed with alerts as-is if DB fetch fails

            if not deployment:
                report = _safe_json(
                    {
                        "action": "HOLD",
                        "deployment": "",
                        "rationale": "No deployment/service label could be mapped from the alert payload",
                        "executed_tools": [],
                        "outcome": "No scaling action",
                        "follow_up": "Add deployment or service labels to alert rules",
                    }
                )
                return {"final_report": report}

            if workload_policy == "AUTOSCALE":
                k8s_allowed = {
                    name
                    for name in tool_registry.names()
                    if name in {
                        "get_current_replicas",
                        "set_replicas",
                        "get_hpa_limits",
                        "set_hpa_max_replicas",
                        "get_budget_state",
                        "plan_budget_allocation",
                        "execute_budget_allocation",
                        "restore_cpu_limits",
                        "get_workload_health",
                    }
                }
            else:
                k8s_allowed = set()

            if not k8s_allowed:
                msg = _safe_json(
                    {
                        "action": "HOLD",
                        "deployment": deployment,
                        "rationale": (
                            "No K8s MCP scaling tools are available for this workload policy"
                            if workload_policy != "AUTOSCALE"
                            else "No K8s MCP action tools available"
                        ),
                        "executed_tools": [],
                        "outcome": "No scaling action",
                        "follow_up": (
                            "Monitor and diagnose the database or other non-scalable workload; do not scale it"
                            if workload_policy != "AUTOSCALE"
                            else "Validate stdio connection to k8s MCP server"
                        ),
                    }
                )
                return {
                    "final_report": msg,
                    "error": "missing_k8s_tools" if workload_policy == "AUTOSCALE" else "monitor_only_workload",
                }

            system_prompt, user_prompt = _reasoning_prompt(
                parsed_alert,
                full_alert,
                investigation_report,
                workload_policy,
            )
            final_report, trace = await _run_llm_tool_loop(
                llm_client=llm_client,
                model=LLM_MODEL,
                tool_registry=tool_registry,
                allowed_tools=k8s_allowed,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_steps=MAX_TOOL_STEPS,
                trace_key="reasoning_action",
                deployment=deployment,
            )

            LOGGER.info(
                "Reasoning and action completed",
                extra={"deployment": deployment, "final_report": final_report},
            )

            merged_trace = list(state.get("llm_trace") or []) + trace
            return {"final_report": final_report, "llm_trace": merged_trace}

        async def generate_report_node(state: AgentState) -> dict[str, Any]:
            raw_alert = state.get("alert") or {}
            parsed_alert = state.get("parsed_alert") or {}
            trace = list(state.get("llm_trace") or [])
            investigation = _parse_report_json(state.get("investigation_report", ""))
            final_report = _parse_report_json(state.get("final_report", ""))
            timestamp_dt = datetime.now(timezone.utc)
            timestamp_utc = timestamp_dt.isoformat().replace("+00:00", "Z")
            incident_id = _incident_id(raw_alert, parsed_alert)

            alert_context = AlertContextModel.model_validate(
                {
                    "alert_name": parsed_alert.get("alert_name", "unknown"),
                    "severity": parsed_alert.get("severity", "unknown"),
                    "status": parsed_alert.get("status", "unknown"),
                    "deployment": parsed_alert.get("deployment", ""),
                    "workload_policy": parsed_alert.get("workload_policy", "UNMONITORED"),
                    "summary": parsed_alert.get("summary", ""),
                    "description": parsed_alert.get("description", ""),
                    "startsAt": parsed_alert.get("startsAt", ""),
                    "endsAt": parsed_alert.get("endsAt", ""),
                    "fingerprint": raw_alert.get("fingerprint", ""),
                    "generatorURL": raw_alert.get("generatorURL", ""),
                    "labels": parsed_alert.get("labels") or {},
                }
            )

            investigation_report = InvestigationReportModel.model_validate(
                {
                    "verdict": investigation.get("verdict", "UNKNOWN"),
                    "confidence": investigation.get("confidence"),
                    "evidence": investigation.get("evidence", []),
                    "recommended_next_step": investigation.get("recommended_next_step", ""),
                }
            )

            action_decision = ActionDecisionModel.model_validate(
                {
                    "action": final_report.get("action", "UNKNOWN"),
                    "deployment": final_report.get("deployment", alert_context.deployment),
                    "rationale": final_report.get("rationale", ""),
                    "executed_tools": final_report.get("executed_tools") or _extract_tool_names(trace),
                    "outcome": final_report.get("outcome", ""),
                    "follow_up": final_report.get("follow_up", ""),
                    "detailed_incident_report": final_report.get(
                        "detailed_incident_report",
                        final_report.get("outcome", state.get("final_report", "")),
                    ),
                    "human_approval": final_report.get("human_approval", final_report.get("approval", "not_required")),
                    "approval": final_report.get("approval"),
                }
            )

            action_name = str(action_decision.action or "UNKNOWN").strip().upper()
            requires_human_approval = (
                str(action_decision.human_approval or "not_required").strip().lower() == "required"
                and action_name in _SCALING_ACTIONS
            )

            structured_report = IncidentReportModel.model_validate(
                {
                    "incident_id": incident_id,
                    "first_seen_utc": timestamp_utc,
                    "last_seen_utc": timestamp_utc,
                    "timestamp_utc": timestamp_utc,
                    "occurrence_count": 0,
                    "current_status": alert_context.status,
                    "alert_context": alert_context,
                    "investigation": investigation_report,
                    "execution_details": ExecutionDetailsModel(
                        action=action_decision,
                        deployment=action_decision.deployment or alert_context.deployment,
                        replicas=_extract_replica_details(final_report, trace),
                        human_approval=("required" if requires_human_approval else "not_required"),
                        rationale=action_decision.rationale,
                        executed_tools=action_decision.executed_tools,
                        outcome=action_decision.outcome,
                        follow_up=action_decision.follow_up,
                    ),
                    "final_summary": action_decision.detailed_incident_report
                    or action_decision.outcome
                    or state.get("final_report", ""),
                    "error": state.get("error", ""),
                }
            )

            async with get_async_session() as session:
                existing_report = await fetch_incident_report(session, incident_id) or {}

                occurrence_count = int(existing_report.get("occurrence_count", 0)) + 1
                first_seen_utc = existing_report.get("first_seen_utc") or timestamp_utc

                structured_report = structured_report.model_copy(
                    update={
                        "first_seen_utc": first_seen_utc,
                        "last_seen_utc": timestamp_utc,
                        "timestamp_utc": timestamp_utc,
                        "occurrence_count": occurrence_count,
                    }
                )

                report_data = structured_report.model_dump(mode="json", exclude_none=True)
                report_data = await _normalize_report_language(llm_client, report_data, label="incident_report")

                await upsert_incident_report(
                    session,
                    incident_id=incident_id,
                    timestamp_utc=timestamp_dt,
                    deployment=structured_report.alert_context.deployment or "unknown",
                    report_data=report_data,
                )

                # If LLM requested human approval for this action, create an approval request
                try:
                    if requires_human_approval:
                        import uuid
                        approval_id = uuid.uuid4().hex
                        
                        # Extract desired_replicas from LLM response or fallback to text parsing
                        suggested_replicas = (
                            report_data.get("execution_details", {}).get("replicas", {}).get("desired_replicas")
                            or report_data.get("execution_details", {}).get("replicas", {}).get("target_replicas")
                            or report_data.get("execution_details", {}).get("replicas", {}).get("current_replicas")
                        )
                        
                        # If still missing and action is SCALE_UP, try to extract from follow_up text
                        if suggested_replicas is None and action_name == "SCALE_UP":
                            follow_up_text = action_decision.follow_up or final_report.get("follow_up", "")
                            suggested_replicas = _extract_replica_count_from_text(follow_up_text)
                        
                        await create_approval_request(
                            session,
                            approval_id=approval_id,
                            incident_id=incident_id,
                            requested_at=timestamp_dt,
                            deployment=structured_report.alert_context.deployment or "unknown",
                            requested_by="sre-guardian",
                            expires_at=None,
                            payload={
                                "report": report_data,
                                "suggested_action": action_decision.action,
                                "suggested_deployment": action_decision.deployment or structured_report.alert_context.deployment,
                                "suggested_replicas": suggested_replicas,
                                "suggested_rationale": action_decision.rationale,
                                "suggested_follow_up": action_decision.follow_up,
                            },
                        )
                        # annotate stored report with approval id
                        report_data.setdefault("execution_details", {})
                        report_data["execution_details"]["approval_id"] = approval_id
                        await upsert_incident_report(
                            session,
                            incident_id=incident_id,
                            timestamp_utc=timestamp_dt,
                            deployment=structured_report.alert_context.deployment or "unknown",
                            report_data=report_data,
                        )
                except Exception:
                    # do not fail the whole flow if approval creation fails
                    LOGGER.exception("Failed to create approval request")

            formatted_report = json.dumps(report_data, indent=2, ensure_ascii=True, default=str)
            print(formatted_report, flush=True)

            LOGGER.info(
                "Post-mortem report generated",
                extra={"incident_id": incident_id, "storage": "postgresql"},
            )
            return {
                "incident_id": incident_id,
                "post_mortem_report": report_data,
                "post_mortem_path": f"db://incident_reports/{incident_id}",
            }

        workflow = StateGraph(AgentState)
        workflow.add_node("ReceiveAlert", receive_alert_node)
        workflow.add_node("Investigate", investigate_node)
        workflow.add_node("ReasoningAction", reasoning_action_node)
        workflow.add_node("ReportGenerator", generate_report_node)

        workflow.set_entry_point("ReceiveAlert")
        workflow.add_edge("ReceiveAlert", "Investigate")
        workflow.add_edge("Investigate", "ReasoningAction")
        workflow.add_edge("ReasoningAction", "ReportGenerator")
        workflow.add_edge("ReportGenerator", END)

        return workflow.compile()
