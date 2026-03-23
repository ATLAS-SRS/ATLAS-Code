#!/usr/bin/env python3
"""
Unit tests for the ATLAS scaling decision logic.
"""

from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scaling_server


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(self.payload)

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return FakeResponse(self.payload)


class FakeContainer:
    def __init__(self, service, project="atlas-code", status="running"):
        self.status = status
        self.labels = {
            "com.docker.compose.service": service,
            "com.docker.compose.project": project,
        }


class FakeContainerCollection:
    def __init__(self, containers):
        self._containers = containers

    def list(self, all=True, filters=None):
        return list(self._containers)


class FakeDockerClient:
    def __init__(self, containers):
        self.containers = FakeContainerCollection(containers)


def make_config(**overrides):
    values = {
        "prometheus_url": "http://prometheus:9090",
        "compose_project_name": "atlas-code",
        "compose_workdir": "/workspace",
        "scaling_mode": "stdio",
        "check_interval_sec": 10,
        "scale_cooldown_sec": 60,
        "min_replicas": 1,
        "max_replicas": 5,
        "target_service": "kafka-consumer",
        "cpu_threshold_percent": 70.0,
        "memory_threshold_percent": 80.0,
        "request_rate_threshold": 100.0,
        "http_host": "127.0.0.1",
        "http_port": 8001,
        "metrics_port": 8002,
        "start_metrics_server": False,
        "llm_enabled": False,
        "lmstudio_base_url": "http://lmstudio:1234/v1",
        "lmstudio_model": "atlas-local",
        "lmstudio_api_key": None,
        "llm_timeout_sec": 5,
        "llm_confidence_threshold": 0.75,
        "llm_max_tokens": 200,
        "llm_temperature": 0.0,
        "llm_connectivity_ttl_sec": 15,
        "decision_history_size": 8,
    }
    values.update(overrides)
    return scaling_server.ScalingConfig(**values)


def make_manager(
    containers=None,
    request_rate=0.0,
    **config_overrides,
):
    config = make_config(**config_overrides)
    payload = {
        "status": "success",
        "data": {"result": [{"value": [time.time(), str(request_rate)]}]},
    }
    docker_client = FakeDockerClient(containers or [])
    session = FakeSession(payload)
    return scaling_server.ScalingManager(
        config=config,
        docker_client=docker_client,
        requests_session=session,
    )


def test_should_scale_up_when_request_rate_crosses_threshold():
    manager = make_manager()
    metrics = {"cpu_percent": 10.0, "memory_percent": 20.0, "request_rate": 150.0}

    assert manager.should_scale_up(metrics) is True


def test_should_scale_down_only_when_above_minimum_replicas():
    manager = make_manager()
    metrics = {"cpu_percent": 10.0, "memory_percent": 15.0, "request_rate": 5.0}

    assert manager.should_scale_down(metrics, current_replicas=1) is False
    assert manager.should_scale_down(metrics, current_replicas=2) is True


def test_build_decision_returns_scale_up_target():
    containers = [FakeContainer("kafka-consumer")]
    manager = make_manager(containers=containers, request_rate=140.0)
    manager.get_system_metrics = lambda: {
        "cpu_percent": 20.0,
        "memory_percent": 25.0,
        "timestamp": time.time(),
    }

    decision = manager.build_decision()

    assert decision["recommendation"] == "scale_up"
    assert decision["current_replicas"] == 1
    assert decision["target_replicas"] == 2
    assert decision["current_metrics"]["request_rate"] == 140.0


def test_evaluate_once_respects_cooldown(monkeypatch):
    manager = make_manager(containers=[FakeContainer("kafka-consumer")])
    manager.build_decision = lambda: {
        "recommendation": "scale_up",
        "current_replicas": 1,
        "target_replicas": 2,
        "cooldown_active": True,
        "cooldown_remaining_sec": 45,
        "current_metrics": {"request_rate": 120.0},
        "effective_decision": {
            "recommendation": "scale_up",
            "target_replicas": 2,
            "blocked_reason": "cooldown_active",
        },
        "decision_source": "blocked",
    }

    def fail_scale(*args, **kwargs):
        raise AssertionError("scale_service must not run during cooldown")

    monkeypatch.setattr(manager, "scale_service", fail_scale)

    decision = manager.evaluate_once()

    assert decision["action"] == "cooldown_blocked"
    assert decision["cooldown_remaining_sec"] == 45


def test_update_thresholds_changes_payload():
    manager = make_manager()

    payload = manager.update_thresholds(cpu_percent=55.0, request_rate=25.0)

    assert payload["new_thresholds"]["cpu_percent"] == 55.0
    assert payload["new_thresholds"]["request_rate"] == 25.0
    assert manager.thresholds["memory_percent"] == 80.0


def test_llm_context_contains_metrics_limits_and_history():
    manager = make_manager()
    manager.action_history.append(
        {"timestamp": 1.0, "service": "kafka-consumer", "to_replicas": 2}
    )
    metrics = {"cpu_percent": 20.0, "memory_percent": 25.0, "request_rate": 50.0}
    rule_based = manager.build_rule_based_decision(metrics, current_replicas=1)

    context = manager.llm_engine.build_context(
        metrics=metrics,
        current_replicas=1,
        rule_based_decision=rule_based,
        thresholds=manager.thresholds,
        limits=manager.limits_payload(),
        action_history=list(manager.action_history),
        target_service=manager.config.target_service,
    )

    assert context["current_metrics"]["request_rate"] == 50.0
    assert context["limits"]["max_replicas"] == 5
    assert context["recent_scaling_actions"][0]["to_replicas"] == 2


def test_llm_validation_blocks_outside_guardrails():
    manager = make_manager(llm_enabled=True)

    result = manager.llm_engine.validate_decision(
        parsed={
            "action": "scale_up",
            "target_replicas": 9,
            "confidence": 0.98,
            "reason": "Traffic spike",
            "risk_flags": [],
            "requires_human_approval": False,
        },
        current_replicas=1,
        limits=manager.limits_payload(),
        cooldown_remaining_sec=0,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "outside_guardrails"


def test_build_decision_falls_back_to_rules_when_llm_output_is_invalid(monkeypatch):
    manager = make_manager(containers=[FakeContainer("kafka-consumer")], llm_enabled=True)
    manager.get_system_metrics = lambda: {
        "cpu_percent": 20.0,
        "memory_percent": 25.0,
        "timestamp": time.time(),
    }

    monkeypatch.setattr(
        manager.llm_engine,
        "request_decision",
        lambda context: {
            "enabled": True,
            "status": "invalid_output",
            "reason": "bad_json",
            "model": "atlas-local",
            "decision": None,
        },
    )

    decision = manager.build_decision()

    assert decision["decision_source"] == "rules"
    assert decision["llm_status"] == "invalid_output"
    assert decision["recommendation"] == "maintain"


def test_build_decision_uses_llm_when_output_is_valid(monkeypatch):
    manager = make_manager(containers=[FakeContainer("kafka-consumer")], llm_enabled=True)
    manager.get_system_metrics = lambda: {
        "cpu_percent": 20.0,
        "memory_percent": 25.0,
        "timestamp": time.time(),
    }

    monkeypatch.setattr(
        manager.llm_engine,
        "request_decision",
        lambda context: {
            "enabled": True,
            "status": "ok",
            "reason": "accepted",
            "model": "atlas-local",
            "decision": {
                "action": "scale_up",
                "target_replicas": 2,
                "confidence": 0.99,
                "reason": "Traffic is above the learned steady state",
                "risk_flags": [],
                "requires_human_approval": False,
            },
        },
    )

    decision = manager.build_decision()

    assert decision["decision_source"] == "llm"
    assert decision["llm_status"] == "ok"
    assert decision["recommendation"] == "scale_up"
    assert decision["effective_decision"]["target_replicas"] == 2
