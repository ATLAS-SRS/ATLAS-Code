#!/usr/bin/env python3
"""
Diagnostic client for the ATLAS scaling daemon.

The production autoscaling loop now lives inside scaling_server.py when
SCALING_MODE=daemon. This script is intentionally lightweight and is useful for
manually watching decisions or inspecting the daemon from a workstation.
"""

import json
import os
import time

import requests
from structured_logger import get_logger

SCALING_AGENT_URL = os.getenv("SCALING_AGENT_URL", "http://localhost:8001")
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "10"))
logger = get_logger("scaling-agent-diagnostic")


def get_json(path: str) -> dict:
    response = requests.get(f"{SCALING_AGENT_URL}{path}", timeout=5)
    response.raise_for_status()
    return response.json()


def print_snapshot() -> None:
    health = get_json("/healthz")
    decision = get_json("/decision")

    logger.info("Scaling snapshot", extra={"health": health, "decision": decision})


def main() -> None:
    logger.info(
        "Starting scaling daemon diagnostic watcher",
        extra={"target": SCALING_AGENT_URL, "polling_seconds": CHECK_INTERVAL_SEC},
    )

    while True:
        try:
            print_snapshot()
        except Exception as exc:
            logger.error("Error talking to scaling daemon", extra={"error": str(exc)})
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
