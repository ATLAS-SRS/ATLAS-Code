#!/usr/bin/env python3
"""
ATLAS scaling service.

This module supports two operating modes:
- daemon: long-running HTTP service with an internal autoscaling loop
- stdio: MCP server for operator-driven manual usage
"""

import asyncio
from collections import deque
import json
import logging
import os
import subprocess
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Optional

import docker
import mcp.types as types
import psutil
import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from mcp.server import Server
from mcp.server.lowlevel import NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from prometheus_client import Gauge, REGISTRY, start_http_server
from pydantic import BaseModel

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
LOGGER = logging.getLogger("atlas.scaling")

CRITICAL_RISK_KEYWORDS = (
    "critical",
    "destructive",
    "policy",
    "approval",
    "security",
    "fraud",
    "uncertain",
    "ambiguous",
    "insufficient",
)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    return float(value)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _get_or_create_gauge(name: str, description: str) -> Gauge:
    try:
        return Gauge(name, description)
    except ValueError as exc:
        if "Duplicated timeseries" not in str(exc):
            raise
        return REGISTRY._names_to_collectors[name]


def _normalize_action(action: str) -> Optional[str]:
    mapping = {
        "maintain": "maintain",
        "scale_up": "scale_up",
        "scaleup": "scale_up",
        "up": "scale_up",
        "scale_down": "scale_down",
        "scaledown": "scale_down",
        "down": "scale_down",
        "escalate": "escalate",
    }
    return mapping.get(str(action).strip().lower())


def _extract_json_candidate(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _risk_flags_are_critical(risk_flags: list[str]) -> bool:
    for risk_flag in risk_flags:
        lowered = risk_flag.lower()
        if any(keyword in lowered for keyword in CRITICAL_RISK_KEYWORDS):
            return True
    return False


@dataclass
class ScalingConfig:
    prometheus_url: str
    compose_project_name: str
    compose_workdir: str
    scaling_mode: str
    check_interval_sec: int
    scale_cooldown_sec: int
    min_replicas: int
    max_replicas: int
    target_service: str
    cpu_threshold_percent: float
    memory_threshold_percent: float
    request_rate_threshold: float
    http_host: str
    http_port: int
    metrics_port: int
    start_metrics_server: bool
    llm_enabled: bool
    lmstudio_base_url: str
    lmstudio_model: str
    lmstudio_api_key: Optional[str]
    llm_timeout_sec: int
    llm_confidence_threshold: float
    llm_max_tokens: int
    llm_temperature: float
    llm_connectivity_ttl_sec: int
    decision_history_size: int


def load_config_from_env() -> ScalingConfig:
    return ScalingConfig(
        prometheus_url=os.getenv("PROMETHEUS_URL", "http://localhost:9090"),
        compose_project_name=os.getenv("COMPOSE_PROJECT_NAME", "atlas-code"),
        compose_workdir=os.getenv("COMPOSE_WORKDIR", "/workspace"),
        scaling_mode=os.getenv("SCALING_MODE", "daemon"),
        check_interval_sec=_env_int("CHECK_INTERVAL_SEC", 60),
        scale_cooldown_sec=_env_int("SCALE_COOLDOWN_SEC", 300),
        min_replicas=_env_int("MIN_REPLICAS", 1),
        max_replicas=_env_int("MAX_REPLICAS", 5),
        target_service=os.getenv("TARGET_SERVICE", "kafka-consumer"),
        cpu_threshold_percent=_env_float("CPU_THRESHOLD_PERCENT", 70.0),
        memory_threshold_percent=_env_float("MEMORY_THRESHOLD_PERCENT", 80.0),
        request_rate_threshold=_env_float("REQUEST_RATE_THRESHOLD", 100.0),
        http_host=os.getenv("SCALING_HTTP_HOST", "0.0.0.0"),
        http_port=_env_int("SCALING_HTTP_PORT", 8001),
        metrics_port=_env_int("SCALING_METRICS_PORT", 8002),
        start_metrics_server=_env_bool("START_METRICS_SERVER", True),
        llm_enabled=_env_bool("LLM_ENABLED", False),
        lmstudio_base_url=os.getenv("LMSTUDIO_BASE_URL", "http://host.docker.internal:1234/v1"),
        lmstudio_model=os.getenv("LMSTUDIO_MODEL", "local-model"),
        lmstudio_api_key=os.getenv("LMSTUDIO_API_KEY"),
        llm_timeout_sec=_env_int("LLM_TIMEOUT_SEC", 8),
        llm_confidence_threshold=_env_float("LLM_CONFIDENCE_THRESHOLD", 0.75),
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", 250),
        llm_temperature=_env_float("LLM_TEMPERATURE", 0.0),
        llm_connectivity_ttl_sec=_env_int("LLM_CONNECTIVITY_TTL_SEC", 15),
        decision_history_size=_env_int("DECISION_HISTORY_SIZE", 8),
    )


class LLMDecisionEngine:
    def __init__(
        self,
        config: ScalingConfig,
        session: Optional[Any] = None,
    ):
        self.config = config
        self.session = session or requests.Session()
        self.last_connectivity_check = 0.0
        self.last_connectivity_payload = {
            "enabled": config.llm_enabled,
            "status": "disabled" if not config.llm_enabled else "unknown",
            "base_url": config.lmstudio_base_url,
            "model": config.lmstudio_model,
        }

    def chat_completions_url(self) -> str:
        base = self.config.lmstudio_base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    def models_url(self) -> str:
        base = self.config.lmstudio_base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return f"{base[:-len('/chat/completions')]}/models"
        if base.endswith("/v1"):
            return f"{base}/models"
        return f"{base}/v1/models"

    def build_context(
        self,
        metrics: dict[str, float],
        current_replicas: int,
        rule_based_decision: dict[str, Any],
        thresholds: dict[str, float],
        limits: dict[str, int],
        action_history: list[dict[str, Any]],
        target_service: str,
    ) -> dict[str, Any]:
        return {
            "target_service": target_service,
            "timestamp": time.time(),
            "current_metrics": metrics,
            "current_replicas": current_replicas,
            "thresholds": thresholds,
            "limits": limits,
            "cooldown_remaining_sec": rule_based_decision.get("cooldown_remaining_sec", 0),
            "rule_based_decision": {
                "recommendation": rule_based_decision.get("recommendation"),
                "target_replicas": rule_based_decision.get("target_replicas"),
                "blocked_reason": rule_based_decision.get("blocked_reason"),
            },
            "recent_scaling_actions": action_history,
        }

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.lmstudio_api_key:
            headers["Authorization"] = f"Bearer {self.config.lmstudio_api_key}"
        return headers

    def _system_prompt(self) -> str:
        return (
            "You are the ATLAS operational scaling controller for a high-risk financial system. "
            "Respond only with a single JSON object. "
            "Choose one action from maintain, scale_up, scale_down, escalate. "
            "Never exceed the given replica limits. "
            "If information is ambiguous, risky, or insufficient, choose escalate or maintain. "
            "The output schema is: "
            '{"action":"maintain|scale_up|scale_down|escalate","target_replicas":1,'
            '"confidence":0.0,"reason":"short explanation","risk_flags":["..."],'
            '"requires_human_approval":false}'
        )

    def _user_prompt(self, context: dict[str, Any]) -> str:
        return (
            "Analyze the following scaling context and return only JSON.\n"
            f"{json.dumps(context, indent=2, sort_keys=True)}"
        )

    def _extract_content(self, payload: dict[str, Any]) -> str:
        choices = payload.get("choices") or []
        if not choices:
            raise ValueError("Missing choices in LM Studio response")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            text_parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            content = "".join(text_parts)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Missing message content in LM Studio response")
        return _extract_json_candidate(content)

    def validate_decision(
        self,
        parsed: dict[str, Any],
        current_replicas: int,
        limits: dict[str, int],
        cooldown_remaining_sec: int,
    ) -> dict[str, Any]:
        action = _normalize_action(parsed.get("action"))
        if action is None:
            return {
                "status": "invalid_output",
                "reason": "invalid_action",
                "decision": None,
            }

        confidence_raw = parsed.get("confidence")
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            return {
                "status": "invalid_output",
                "reason": "invalid_confidence",
                "decision": None,
            }

        reason = str(parsed.get("reason", "")).strip()
        if not reason:
            return {
                "status": "invalid_output",
                "reason": "missing_reason",
                "decision": None,
            }

        risk_flags = parsed.get("risk_flags", [])
        if not isinstance(risk_flags, list) or not all(
            isinstance(flag, str) for flag in risk_flags
        ):
            return {
                "status": "invalid_output",
                "reason": "invalid_risk_flags",
                "decision": None,
            }

        requires_human_approval = bool(parsed.get("requires_human_approval", False))

        if action == "maintain":
            target_replicas = current_replicas
        else:
            try:
                target_replicas = int(parsed.get("target_replicas"))
            except (TypeError, ValueError):
                return {
                    "status": "invalid_output",
                    "reason": "invalid_target_replicas",
                    "decision": None,
                }

        decision = {
            "action": action,
            "target_replicas": target_replicas,
            "confidence": confidence,
            "reason": reason,
            "risk_flags": risk_flags,
            "requires_human_approval": requires_human_approval,
        }

        if action == "escalate":
            return {
                "status": "blocked",
                "reason": "llm_requested_escalation",
                "decision": decision,
            }

        if target_replicas < limits["min_replicas"] or target_replicas > limits["max_replicas"]:
            return {
                "status": "blocked",
                "reason": "outside_guardrails",
                "decision": decision,
            }

        if action == "scale_up" and target_replicas <= current_replicas:
            return {
                "status": "blocked",
                "reason": "non_increasing_scale_up",
                "decision": decision,
            }

        if action == "scale_down" and target_replicas >= current_replicas:
            return {
                "status": "blocked",
                "reason": "non_decreasing_scale_down",
                "decision": decision,
            }

        if requires_human_approval:
            return {
                "status": "blocked",
                "reason": "requires_human_approval",
                "decision": decision,
            }

        if confidence < self.config.llm_confidence_threshold:
            return {
                "status": "blocked",
                "reason": "confidence_below_threshold",
                "decision": decision,
            }

        if _risk_flags_are_critical(risk_flags):
            return {
                "status": "blocked",
                "reason": "critical_risk_flag",
                "decision": decision,
            }

        if cooldown_remaining_sec > 0 and action != "maintain":
            return {
                "status": "blocked",
                "reason": "cooldown_active",
                "decision": decision,
            }

        return {
            "status": "ok",
            "reason": "accepted",
            "decision": decision,
        }

    def request_decision(self, context: dict[str, Any]) -> dict[str, Any]:
        if not self.config.llm_enabled:
            return {
                "enabled": False,
                "status": "disabled",
                "reason": "llm_disabled",
                "model": self.config.lmstudio_model,
                "decision": None,
            }

        payload = {
            "model": self.config.lmstudio_model,
            "temperature": self.config.llm_temperature,
            "max_tokens": self.config.llm_max_tokens,
            "response_format": {"type": "text"},
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": self._user_prompt(context)},
            ],
        }

        try:
            response = self.session.post(
                self.chat_completions_url(),
                headers=self._headers(),
                json=payload,
                timeout=self.config.llm_timeout_sec,
            )
            response.raise_for_status()
            body = response.json()
            raw_content = self._extract_content(body)
            parsed = json.loads(raw_content)
        except requests.Timeout:
            return {
                "enabled": True,
                "status": "timeout",
                "reason": "llm_timeout",
                "model": self.config.lmstudio_model,
                "decision": None,
            }
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            LOGGER.warning("LM Studio request failed: %s", exc)
            return {
                "enabled": True,
                "status": "invalid_output",
                "reason": str(exc),
                "model": self.config.lmstudio_model,
                "decision": None,
            }

        validation = self.validate_decision(
            parsed=parsed,
            current_replicas=context["current_replicas"],
            limits=context["limits"],
            cooldown_remaining_sec=context["cooldown_remaining_sec"],
        )
        validation.update(
            {
                "enabled": True,
                "model": self.config.lmstudio_model,
                "raw_content": raw_content,
            }
        )
        return validation

    def connectivity_status(self, force: bool = False) -> dict[str, Any]:
        if not self.config.llm_enabled:
            return {
                "enabled": False,
                "status": "disabled",
                "base_url": self.config.lmstudio_base_url,
                "model": self.config.lmstudio_model,
            }

        if (
            not force
            and time.time() - self.last_connectivity_check < self.config.llm_connectivity_ttl_sec
        ):
            return dict(self.last_connectivity_payload)

        payload = {
            "enabled": True,
            "status": "unreachable",
            "base_url": self.config.lmstudio_base_url,
            "model": self.config.lmstudio_model,
        }
        try:
            response = self.session.get(
                self.models_url(),
                headers=self._headers(),
                timeout=min(2, self.config.llm_timeout_sec),
            )
            response.raise_for_status()
            payload["status"] = "reachable"
        except requests.Timeout:
            payload["status"] = "timeout"
        except requests.RequestException as exc:
            payload["status"] = f"error:{exc.__class__.__name__}"

        self.last_connectivity_check = time.time()
        self.last_connectivity_payload = payload
        return dict(payload)


class ScalingManager:
    def __init__(
        self,
        config: ScalingConfig,
        docker_client: Optional[Any] = None,
        requests_session: Optional[Any] = None,
        llm_engine: Optional[LLMDecisionEngine] = None,
    ):
        self.config = config
        self.docker_client = docker_client or docker.from_env()
        self.requests_session = requests_session or requests.Session()
        self.last_scale_time = 0.0
        self.action_history: deque[dict[str, Any]] = deque(
            maxlen=config.decision_history_size
        )
        self.last_llm_status = "disabled" if not config.llm_enabled else "unknown"
        self.thresholds = {
            "cpu_percent": config.cpu_threshold_percent,
            "memory_percent": config.memory_threshold_percent,
            "request_rate": config.request_rate_threshold,
        }
        self.llm_engine = llm_engine or LLMDecisionEngine(
            config=config,
            session=self.requests_session,
        )
        self.metrics = {
            "cpu_usage": _get_or_create_gauge(
                "atlas_cpu_usage_percent", "CPU usage percentage"
            ),
            "memory_usage": _get_or_create_gauge(
                "atlas_memory_usage_percent", "Memory usage percentage"
            ),
            "request_rate": _get_or_create_gauge(
                "atlas_request_rate_per_minute", "Request rate per minute"
            ),
            "active_containers": _get_or_create_gauge(
                "atlas_active_containers", "Number of active containers"
            ),
            "last_target_replicas": _get_or_create_gauge(
                "atlas_last_target_replicas", "Last requested replica count"
            ),
        }

    def limits_payload(self) -> dict[str, int]:
        return {
            "min_replicas": self.config.min_replicas,
            "max_replicas": self.config.max_replicas,
            "cooldown_sec": self.config.scale_cooldown_sec,
        }

    def get_thresholds_payload(self) -> dict[str, Any]:
        return {
            "thresholds": dict(self.thresholds),
            "limits": self.limits_payload(),
            "target_service": self.config.target_service,
            "llm": {
                "enabled": self.config.llm_enabled,
                "model": self.config.lmstudio_model,
                "confidence_threshold": self.config.llm_confidence_threshold,
            },
        }

    def get_health_payload(self) -> dict[str, Any]:
        llm_health = self.llm_engine.connectivity_status()
        llm_health["last_decision_status"] = self.last_llm_status
        return {
            "status": "ok",
            "mode": self.config.scaling_mode,
            "target_service": self.config.target_service,
            "compose_workdir": self.config.compose_workdir,
            "llm": llm_health,
        }

    def update_thresholds(
        self,
        cpu_percent: Optional[float] = None,
        memory_percent: Optional[float] = None,
        request_rate: Optional[float] = None,
    ) -> dict[str, Any]:
        if cpu_percent is not None:
            self.thresholds["cpu_percent"] = float(cpu_percent)
        if memory_percent is not None:
            self.thresholds["memory_percent"] = float(memory_percent)
        if request_rate is not None:
            self.thresholds["request_rate"] = float(request_rate)

        return {
            "message": "Thresholds updated successfully",
            "new_thresholds": dict(self.thresholds),
        }

    def get_system_metrics(self) -> dict[str, float]:
        cpu_percent = psutil.cpu_percent(interval=0.2)
        memory = psutil.virtual_memory()
        return {
            "cpu_percent": cpu_percent,
            "memory_percent": memory.percent,
            "timestamp": time.time(),
        }

    def get_prometheus_metrics(self) -> dict[str, float]:
        query = "sum(rate(http_requests_total[5m])) * 60"
        try:
            response = self.requests_session.get(
                f"{self.config.prometheus_url}/api/v1/query",
                params={"query": query},
                timeout=5,
            )
            response.raise_for_status()
            data = response.json()
            request_rate = 0.0
            results = data.get("data", {}).get("result", [])
            if data.get("status") == "success" and results:
                request_rate = float(results[0]["value"][1])
            return {"request_rate": request_rate}
        except Exception as exc:
            LOGGER.warning("Error fetching Prometheus metrics: %s", exc)
            return {"request_rate": 0.0}

    def get_container_count(self, service_name: Optional[str] = None) -> int:
        target_service = service_name or self.config.target_service
        try:
            containers = self.docker_client.containers.list(
                all=True,
                filters={"label": f"com.docker.compose.service={target_service}"},
            )
            running = [
                container
                for container in containers
                if (container.labels or {}).get("com.docker.compose.project")
                == self.config.compose_project_name
                and container.status == "running"
            ]
            count = len(running)
            self.metrics["active_containers"].set(count)
            return count
        except Exception as exc:
            LOGGER.warning("Error getting container count for %s: %s", target_service, exc)
            return 0

    def collect_current_metrics(self) -> dict[str, float]:
        metrics = {**self.get_system_metrics(), **self.get_prometheus_metrics()}
        self.metrics["cpu_usage"].set(metrics.get("cpu_percent", 0.0))
        self.metrics["memory_usage"].set(metrics.get("memory_percent", 0.0))
        self.metrics["request_rate"].set(metrics.get("request_rate", 0.0))
        return metrics

    def should_scale_up(self, metrics: dict[str, float]) -> bool:
        return (
            metrics.get("cpu_percent", 0.0) > self.thresholds["cpu_percent"]
            or metrics.get("memory_percent", 0.0) > self.thresholds["memory_percent"]
            or metrics.get("request_rate", 0.0) > self.thresholds["request_rate"]
        )

    def should_scale_down(self, metrics: dict[str, float], current_replicas: int) -> bool:
        if current_replicas <= self.config.min_replicas:
            return False
        return (
            metrics.get("cpu_percent", 0.0) < self.thresholds["cpu_percent"] * 0.3
            and metrics.get("memory_percent", 0.0)
            < self.thresholds["memory_percent"] * 0.3
            and metrics.get("request_rate", 0.0) < self.thresholds["request_rate"] * 0.3
        )

    def cooldown_remaining_sec(self) -> int:
        if self.last_scale_time == 0:
            return 0
        remaining = self.config.scale_cooldown_sec - (time.time() - self.last_scale_time)
        return max(0, int(remaining))

    def build_rule_based_decision(
        self,
        metrics: dict[str, float],
        current_replicas: int,
    ) -> dict[str, Any]:
        should_scale_up = self.should_scale_up(metrics)
        should_scale_down = self.should_scale_down(metrics, current_replicas)
        recommendation = "maintain"
        target_replicas = current_replicas

        if current_replicas < self.config.min_replicas:
            recommendation = "scale_up"
            target_replicas = self.config.min_replicas
        elif should_scale_up and current_replicas < self.config.max_replicas:
            recommendation = "scale_up"
            target_replicas = current_replicas + 1
        elif should_scale_down and current_replicas > self.config.min_replicas:
            recommendation = "scale_down"
            target_replicas = current_replicas - 1

        cooldown_remaining = self.cooldown_remaining_sec()
        cooldown_active = cooldown_remaining > 0
        blocked_reason = None
        if cooldown_active and recommendation != "maintain":
            blocked_reason = "cooldown_active"

        return {
            "recommendation": recommendation,
            "target_replicas": target_replicas,
            "current_replicas": current_replicas,
            "current_metrics": metrics,
            "should_scale_up": should_scale_up,
            "should_scale_down": should_scale_down,
            "cooldown_active": cooldown_active,
            "cooldown_remaining_sec": cooldown_remaining,
            "blocked_reason": blocked_reason,
            "reason": "rule_based_thresholds",
            "thresholds": dict(self.thresholds),
        }

    def _llm_decision_payload(
        self,
        metrics: dict[str, float],
        current_replicas: int,
        rule_based_decision: dict[str, Any],
    ) -> dict[str, Any]:
        context = self.llm_engine.build_context(
            metrics=metrics,
            current_replicas=current_replicas,
            rule_based_decision=rule_based_decision,
            thresholds=dict(self.thresholds),
            limits=self.limits_payload(),
            action_history=list(self.action_history),
            target_service=self.config.target_service,
        )
        llm_result = self.llm_engine.request_decision(context)
        self.last_llm_status = llm_result["status"]
        return llm_result

    def _source_from_rule(self, rule_based_decision: dict[str, Any]) -> str:
        if (
            rule_based_decision["recommendation"] != "maintain"
            and rule_based_decision["cooldown_active"]
        ):
            return "blocked"
        return "rules"

    def _effective_decision_from_rule(self, rule_based_decision: dict[str, Any]) -> dict[str, Any]:
        return {
            "recommendation": rule_based_decision["recommendation"],
            "target_replicas": rule_based_decision["target_replicas"],
            "reason": rule_based_decision.get("reason", "rule_based_thresholds"),
            "blocked_reason": rule_based_decision.get("blocked_reason"),
        }

    def _effective_decision_from_llm(self, llm_decision: dict[str, Any]) -> dict[str, Any]:
        recommendation = "maintain" if llm_decision["action"] == "maintain" else llm_decision["action"]
        return {
            "recommendation": recommendation,
            "target_replicas": llm_decision["target_replicas"],
            "reason": llm_decision["reason"],
            "blocked_reason": None,
            "confidence": llm_decision["confidence"],
            "risk_flags": llm_decision["risk_flags"],
            "requires_human_approval": llm_decision["requires_human_approval"],
        }

    def resolve_effective_decision(
        self,
        rule_based_decision: dict[str, Any],
        llm_result: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        llm_decision = llm_result.get("decision")

        if llm_result["status"] == "ok" and llm_decision is not None:
            return self._effective_decision_from_llm(llm_decision), "llm"

        rule_effective = self._effective_decision_from_rule(rule_based_decision)

        if llm_result["status"] == "blocked" and self._source_from_rule(rule_based_decision) == "blocked":
            rule_effective["blocked_reason"] = (
                rule_effective.get("blocked_reason") or llm_result.get("reason")
            )
            return rule_effective, "blocked"

        return rule_effective, self._source_from_rule(rule_based_decision)

    def build_decision(
        self, metrics: Optional[dict[str, float]] = None
    ) -> dict[str, Any]:
        current_metrics = metrics or self.collect_current_metrics()
        current_replicas = self.get_container_count(self.config.target_service)
        rule_based_decision = self.build_rule_based_decision(
            current_metrics,
            current_replicas,
        )
        llm_result = self._llm_decision_payload(
            current_metrics,
            current_replicas,
            rule_based_decision,
        )
        effective_decision, decision_source = self.resolve_effective_decision(
            rule_based_decision,
            llm_result,
        )
        self.metrics["last_target_replicas"].set(effective_decision["target_replicas"])

        return {
            "current_metrics": current_metrics,
            "current_replicas": current_replicas,
            "current_kafka_replicas": current_replicas,
            "should_scale_up": rule_based_decision["should_scale_up"],
            "should_scale_down": rule_based_decision["should_scale_down"],
            "recommendation": effective_decision["recommendation"],
            "target_replicas": effective_decision["target_replicas"],
            "cooldown_active": rule_based_decision["cooldown_active"],
            "cooldown_remaining_sec": rule_based_decision["cooldown_remaining_sec"],
            "thresholds": dict(self.thresholds),
            "rule_based_decision": rule_based_decision,
            "llm_decision": llm_result.get("decision"),
            "effective_decision": effective_decision,
            "decision_source": decision_source,
            "llm_status": llm_result["status"],
            "llm_reason": llm_result.get("reason"),
            "llm_model": llm_result.get("model"),
        }

    def _record_action(
        self,
        service_name: str,
        replicas: int,
        reason: str,
        previous_replicas: int,
        current_replicas: int,
    ) -> None:
        self.action_history.append(
            {
                "timestamp": time.time(),
                "service": service_name,
                "reason": reason,
                "from_replicas": previous_replicas,
                "to_replicas": current_replicas or replicas,
            }
        )

    def scale_service(
        self,
        service_name: str,
        replicas: int,
        reason: str = "manual",
        enforce_cooldown: bool = False,
    ) -> dict[str, Any]:
        if replicas < self.config.min_replicas or replicas > self.config.max_replicas:
            return {
                "success": False,
                "message": (
                    f"Requested replicas {replicas} outside guardrails "
                    f"[{self.config.min_replicas}, {self.config.max_replicas}]"
                ),
            }

        current_replicas = self.get_container_count(service_name)
        if replicas == current_replicas:
            return {
                "success": True,
                "message": f"{service_name} already at {replicas} replicas",
                "current_replicas": current_replicas,
                "reason": reason,
            }

        if enforce_cooldown and self.cooldown_remaining_sec() > 0:
            return {
                "success": False,
                "message": (
                    f"Cooldown active for {self.cooldown_remaining_sec()} more seconds"
                ),
                "reason": reason,
            }

        cmd = [
            "docker-compose",
            "up",
            "-d",
            "--no-deps",
            "--scale",
            f"{service_name}={replicas}",
            service_name,
        ]
        env = os.environ.copy()
        env["COMPOSE_PROJECT_NAME"] = self.config.compose_project_name

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=self.config.compose_workdir,
                env=env,
            )
        except Exception as exc:
            return {
                "success": False,
                "message": f"Error scaling service: {exc}",
                "reason": reason,
            }

        success = result.returncode == 0
        payload = {
            "success": success,
            "message": (
                f"Successfully scaled {service_name} to {replicas} replicas"
                if success
                else f"Failed to scale {service_name}: {result.stderr.strip()}"
            ),
            "reason": reason,
            "target_replicas": replicas,
            "command": cmd,
            "output": result.stdout.strip(),
            "error": result.stderr.strip(),
        }

        if success:
            self.last_scale_time = time.time()
            self.metrics["last_target_replicas"].set(replicas)
            payload["current_replicas"] = self.get_container_count(service_name)
            self._record_action(
                service_name=service_name,
                replicas=replicas,
                reason=reason,
                previous_replicas=current_replicas,
                current_replicas=payload["current_replicas"],
            )

        return payload

    def evaluate_once(self) -> dict[str, Any]:
        decision = self.build_decision()
        effective = decision["effective_decision"]
        target_replicas = effective["target_replicas"]
        current_replicas = decision["current_replicas"]

        if decision["decision_source"] == "blocked":
            blocked_reason = effective.get("blocked_reason") or decision.get("llm_reason")
            decision["action"] = (
                "cooldown_blocked" if blocked_reason == "cooldown_active" else "policy_blocked"
            )
            return decision

        if (
            effective["recommendation"] == "maintain"
            or target_replicas == current_replicas
        ):
            decision["action"] = "noop"
            return decision

        scale_result = self.scale_service(
            self.config.target_service,
            target_replicas,
            reason=f"auto:{decision['decision_source']}:{effective['recommendation']}",
            enforce_cooldown=False,
        )
        decision["scale_result"] = scale_result
        decision["action"] = "scaled" if scale_result.get("success") else "scale_failed"
        decision["cooldown_active"] = self.cooldown_remaining_sec() > 0
        decision["cooldown_remaining_sec"] = self.cooldown_remaining_sec()
        return decision


class ScaleRequest(BaseModel):
    replicas: int
    reason: Optional[str] = None


class ThresholdUpdateRequest(BaseModel):
    cpu_percent: Optional[float] = None
    memory_percent: Optional[float] = None
    request_rate: Optional[float] = None


async def run_autoscaling_loop(
    manager: ScalingManager, stop_event: asyncio.Event
) -> None:
    LOGGER.info(
        "Starting autoscaling loop for %s (check=%ss cooldown=%ss llm_enabled=%s)",
        manager.config.target_service,
        manager.config.check_interval_sec,
        manager.config.scale_cooldown_sec,
        manager.config.llm_enabled,
    )
    while not stop_event.is_set():
        try:
            decision = await asyncio.to_thread(manager.evaluate_once)
            LOGGER.info(
                "Autoscaling tick source=%s llm_status=%s recommendation=%s action=%s current=%s target=%s request_rate=%.2f",
                decision.get("decision_source"),
                decision.get("llm_status"),
                decision.get("recommendation"),
                decision.get("action"),
                decision.get("current_replicas"),
                decision.get("target_replicas"),
                decision.get("current_metrics", {}).get("request_rate", 0.0),
            )
        except Exception as exc:
            LOGGER.exception("Error in autoscaling loop: %s", exc)

        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=manager.config.check_interval_sec
            )
        except asyncio.TimeoutError:
            continue


def create_daemon_app(
    manager: Optional[ScalingManager] = None,
    config: Optional[ScalingConfig] = None,
) -> FastAPI:
    config = config or load_config_from_env()
    manager = manager or ScalingManager(config)

    metrics_started = False
    stop_event = asyncio.Event()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal metrics_started
        task = None

        if config.start_metrics_server and not metrics_started:
            start_http_server(config.metrics_port)
            metrics_started = True

        if config.scaling_mode == "daemon":
            task = asyncio.create_task(run_autoscaling_loop(manager, stop_event))

        yield

        if task is not None:
            stop_event.set()
            await task

    app = FastAPI(title="ATLAS Scaling Agent", lifespan=lifespan)
    app.state.manager = manager
    app.state.config = config

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        if hasattr(manager, "get_health_payload"):
            return manager.get_health_payload()
        return {
            "status": "ok",
            "mode": config.scaling_mode,
            "target_service": config.target_service,
            "compose_workdir": config.compose_workdir,
            "llm": {"enabled": config.llm_enabled, "status": "unknown"},
        }

    @app.get("/thresholds")
    async def get_thresholds() -> dict[str, Any]:
        return manager.get_thresholds_payload()

    @app.post("/thresholds")
    async def update_thresholds(request: ThresholdUpdateRequest) -> dict[str, Any]:
        return manager.update_thresholds(
            cpu_percent=request.cpu_percent,
            memory_percent=request.memory_percent,
            request_rate=request.request_rate,
        )

    @app.get("/decision")
    async def get_decision() -> dict[str, Any]:
        return await asyncio.to_thread(manager.build_decision)

    @app.post("/scale")
    async def scale(request: ScaleRequest) -> dict[str, Any]:
        result = await asyncio.to_thread(
            manager.scale_service,
            config.target_service,
            request.replicas,
            request.reason or "manual_api",
            False,
        )
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result["message"])
        return result

    return app


MCP_CONTEXT: dict[str, Optional[ScalingManager]] = {"manager": None}
server = Server("scaling-agent")


def _mcp_manager() -> ScalingManager:
    manager = MCP_CONTEXT["manager"]
    if manager is None:
        raise RuntimeError("Scaling manager not initialized")
    return manager


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_current_metrics",
            description="Get current system and application metrics",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="check_scaling_decision",
            description="Check if scaling actions are needed based on current metrics",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="scale_kafka_consumer",
            description="Scale the kafka-consumer service to the specified number of replicas",
            inputSchema={
                "type": "object",
                "properties": {
                    "replicas": {
                        "type": "integer",
                        "description": "Number of replicas to scale to",
                        "minimum": 1,
                        "maximum": 5,
                    }
                },
                "required": ["replicas"],
            },
        ),
        types.Tool(
            name="get_scaling_thresholds",
            description="Get the current scaling thresholds",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="update_scaling_thresholds",
            description="Update scaling thresholds",
            inputSchema={
                "type": "object",
                "properties": {
                    "cpu_percent": {"type": "number", "minimum": 0, "maximum": 100},
                    "memory_percent": {"type": "number", "minimum": 0, "maximum": 100},
                    "request_rate": {"type": "number", "minimum": 0},
                },
                "required": [],
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any]
) -> list[types.TextContent]:
    manager = _mcp_manager()

    if name == "get_current_metrics":
        payload = manager.collect_current_metrics()
    elif name == "check_scaling_decision":
        payload = manager.build_decision()
    elif name == "scale_kafka_consumer":
        payload = manager.scale_service(
            manager.config.target_service,
            arguments.get("replicas", manager.config.min_replicas),
            reason="manual_mcp",
            enforce_cooldown=False,
        )
    elif name == "get_scaling_thresholds":
        payload = manager.get_thresholds_payload()
    elif name == "update_scaling_thresholds":
        payload = manager.update_thresholds(
            cpu_percent=arguments.get("cpu_percent"),
            memory_percent=arguments.get("memory_percent"),
            request_rate=arguments.get("request_rate"),
        )
    else:
        raise ValueError(f"Unknown tool: {name}")

    return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]


async def run_stdio_mcp(manager: ScalingManager, config: ScalingConfig) -> None:
    MCP_CONTEXT["manager"] = manager

    if config.start_metrics_server:
        start_http_server(config.metrics_port)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="scaling-agent",
                server_version="0.3.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def main() -> None:
    config = load_config_from_env()
    manager = ScalingManager(config)

    if config.scaling_mode == "stdio":
        LOGGER.info("Starting ATLAS scaling agent in stdio MCP mode")
        asyncio.run(run_stdio_mcp(manager, config))
        return

    if config.scaling_mode != "daemon":
        raise SystemExit(f"Unsupported SCALING_MODE: {config.scaling_mode}")

    LOGGER.info(
        "Starting ATLAS scaling daemon on %s:%s for service %s",
        config.http_host,
        config.http_port,
        config.target_service,
    )
    uvicorn.run(
        create_daemon_app(manager=manager, config=config),
        host=config.http_host,
        port=config.http_port,
    )


if __name__ == "__main__":
    main()
