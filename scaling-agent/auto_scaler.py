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

SCALING_AGENT_URL = os.getenv("SCALING_AGENT_URL", "http://localhost:8001")
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "10"))


def get_json(path: str) -> dict:
    response = requests.get(f"{SCALING_AGENT_URL}{path}", timeout=5)
    response.raise_for_status()
    return response.json()


def print_snapshot() -> None:
    health = get_json("/healthz")
    decision = get_json("/decision")

    print(json.dumps({"health": health, "decision": decision}, indent=2))


def main() -> None:
    print("ATLAS scaling daemon diagnostic watcher")
    print(f"Target: {SCALING_AGENT_URL}")
    print(f"Polling every {CHECK_INTERVAL_SEC} seconds")

    while True:
        try:
            print_snapshot()
        except Exception as exc:
            print(f"Error talking to scaling daemon: {exc}")
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
