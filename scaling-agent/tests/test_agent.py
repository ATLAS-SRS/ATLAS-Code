#!/usr/bin/env python3
"""
Integration and smoke tests for the ATLAS scaling service.
"""

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
import requests
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scaling_server


class FakeContainerCollection:
    def list(self, all=True, filters=None):
        return []


class FakeDockerClient:
    def __init__(self):
        self.containers = FakeContainerCollection()


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


def test_scale_service_uses_compose_workdir_and_command(monkeypatch):
    manager = scaling_server.ScalingManager(
        config=make_config(),
        docker_client=FakeDockerClient(),
        requests_session=requests.Session(),
    )

    call_state = {"count": 0}

    def fake_container_count(service_name):
        call_state["count"] += 1
        return 1 if call_state["count"] == 1 else 2

    monkeypatch.setattr(manager, "get_container_count", fake_container_count)

    recorded = {}

    def fake_run(cmd, capture_output, text, cwd, env):
        recorded["cmd"] = cmd
        recorded["cwd"] = cwd
        recorded["env"] = env
        return subprocess.CompletedProcess(cmd, 0, stdout="scaled", stderr="")

    monkeypatch.setattr(scaling_server.subprocess, "run", fake_run)

    result = manager.scale_service("kafka-consumer", 2, reason="manual_test")

    assert result["success"] is True
    assert recorded["cwd"] == "/workspace"
    assert recorded["env"]["COMPOSE_PROJECT_NAME"] == "atlas-code"
    assert recorded["cmd"] == [
        "docker-compose",
        "up",
        "-d",
        "--no-deps",
        "--scale",
        "kafka-consumer=2",
        "kafka-consumer",
    ]


def test_scale_service_rejects_outside_guardrails():
    manager = scaling_server.ScalingManager(
        config=make_config(),
        docker_client=FakeDockerClient(),
        requests_session=requests.Session(),
    )

    result = manager.scale_service("kafka-consumer", 9)

    assert result["success"] is False
    assert "outside guardrails" in result["message"]


def test_http_endpoints_expose_decision_and_manual_scale():
    config = make_config()

    class FakeManager:
        def __init__(self):
            self.config = config
            self.scale_calls = []
            self.threshold_updates = []

        def get_health_payload(self):
            return {
                "status": "ok",
                "mode": config.scaling_mode,
                "target_service": config.target_service,
                "compose_workdir": config.compose_workdir,
                "llm": {"enabled": False, "status": "disabled"},
            }

        def get_thresholds_payload(self):
            return {
                "thresholds": {"cpu_percent": 70.0, "memory_percent": 80.0, "request_rate": 100.0},
                "limits": {"min_replicas": 1, "max_replicas": 5, "cooldown_sec": 60},
                "target_service": "kafka-consumer",
                "llm": {"enabled": False, "model": "atlas-local", "confidence_threshold": 0.75},
            }

        def update_thresholds(self, cpu_percent=None, memory_percent=None, request_rate=None):
            self.threshold_updates.append((cpu_percent, memory_percent, request_rate))
            return {"message": "Thresholds updated successfully"}

        def build_decision(self):
            return {
                "recommendation": "maintain",
                "current_replicas": 1,
                "target_replicas": 1,
                "cooldown_active": False,
                "cooldown_remaining_sec": 0,
                "current_metrics": {"request_rate": 12.0},
                "llm_decision": None,
                "rule_based_decision": {"recommendation": "maintain", "target_replicas": 1},
                "effective_decision": {"recommendation": "maintain", "target_replicas": 1},
                "decision_source": "rules",
                "llm_status": "disabled",
            }

        def scale_service(self, service_name, replicas, reason, enforce_cooldown):
            self.scale_calls.append((service_name, replicas, reason, enforce_cooldown))
            return {"success": True, "target_replicas": replicas, "reason": reason}

    app = scaling_server.create_daemon_app(manager=FakeManager(), config=config)

    with TestClient(app) as client:
        health = client.get("/healthz")
        thresholds = client.get("/thresholds")
        decision = client.get("/decision")
        scale = client.post("/scale", json={"replicas": 2, "reason": "manual_http"})
        update = client.post("/thresholds", json={"request_rate": 15})

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert thresholds.status_code == 200
    assert thresholds.json()["target_service"] == "kafka-consumer"
    assert decision.status_code == 200
    assert decision.json()["recommendation"] == "maintain"
    assert decision.json()["decision_source"] == "rules"
    assert decision.json()["llm_status"] == "disabled"
    assert scale.status_code == 200
    assert scale.json()["target_replicas"] == 2
    assert update.status_code == 200


def test_http_decision_surfaces_llm_metadata():
    config = make_config(llm_enabled=True)

    class FakeManager:
        def get_health_payload(self):
            return {
                "status": "ok",
                "mode": "daemon",
                "target_service": "kafka-consumer",
                "compose_workdir": "/workspace",
                "llm": {"enabled": True, "status": "reachable", "model": "atlas-local"},
            }

        def get_thresholds_payload(self):
            return {
                "thresholds": {"cpu_percent": 70.0, "memory_percent": 80.0, "request_rate": 100.0},
                "limits": {"min_replicas": 1, "max_replicas": 5, "cooldown_sec": 60},
                "target_service": "kafka-consumer",
                "llm": {"enabled": True, "model": "atlas-local", "confidence_threshold": 0.75},
            }

        def update_thresholds(self, cpu_percent=None, memory_percent=None, request_rate=None):
            return {"message": "Thresholds updated successfully"}

        def build_decision(self):
            return {
                "recommendation": "scale_up",
                "current_replicas": 1,
                "target_replicas": 2,
                "cooldown_active": False,
                "cooldown_remaining_sec": 0,
                "current_metrics": {"request_rate": 12.0},
                "llm_decision": {
                    "action": "scale_up",
                    "target_replicas": 2,
                    "confidence": 0.99,
                    "reason": "Model detected sustained load",
                    "risk_flags": [],
                    "requires_human_approval": False,
                },
                "rule_based_decision": {"recommendation": "maintain", "target_replicas": 1},
                "effective_decision": {"recommendation": "scale_up", "target_replicas": 2},
                "decision_source": "llm",
                "llm_status": "ok",
                "llm_model": "atlas-local",
            }

        def scale_service(self, service_name, replicas, reason, enforce_cooldown):
            return {"success": True, "target_replicas": replicas, "reason": reason}

    app = scaling_server.create_daemon_app(manager=FakeManager(), config=config)

    with TestClient(app) as client:
        decision = client.get("/decision")
        health = client.get("/healthz")

    assert decision.status_code == 200
    assert decision.json()["decision_source"] == "llm"
    assert decision.json()["llm_status"] == "ok"
    assert decision.json()["llm_decision"]["confidence"] == 0.99
    assert health.status_code == 200
    assert health.json()["llm"]["enabled"] is True


@pytest.mark.skipif(
    os.getenv("ATLAS_ENABLE_DOCKER_SMOKE") != "1",
    reason="Docker smoke tests are disabled",
)
def test_docker_smoke_scaling_agent_health_and_manual_scale():
    ps = subprocess.run(
        ["docker", "compose", "ps", "scaling-agent"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Restarting" not in ps.stdout
    assert "Up" in ps.stdout

    health = requests.get("http://localhost:8001/healthz", timeout=5)
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    targets = requests.get("http://localhost:9090/api/v1/targets", timeout=5)
    assert targets.status_code == 200
    scaling_target = next(
        target
        for target in targets.json()["data"]["activeTargets"]
        if target["labels"]["job"] == "scaling-agent"
    )
    assert scaling_target["health"] == "up"

    scale_up = requests.post(
        "http://localhost:8001/scale",
        json={"replicas": 2, "reason": "pytest_smoke"},
        timeout=10,
    )
    assert scale_up.status_code == 200

    deadline = time.time() + 30
    while time.time() < deadline:
        decision = requests.get("http://localhost:8001/decision", timeout=5).json()
        if decision["current_replicas"] == 2:
            break
        time.sleep(2)
    else:
        raise AssertionError("kafka-consumer did not scale to 2 replicas")

    scale_down = requests.post(
        "http://localhost:8001/scale",
        json={"replicas": 1, "reason": "pytest_smoke_cleanup"},
        timeout=10,
    )
    assert scale_down.status_code == 200
