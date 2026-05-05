from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def parse_keyed_ints(raw: str, allowed_keys: set[str], default_value: int) -> dict[str, int]:
    values = {key: default_value for key in allowed_keys}
    if not raw.strip():
        return values

    for item in raw.split(","):
        token = item.strip()
        if not token or "=" not in token:
            continue

        key, value = token.split("=", 1)
        key = key.strip()
        if key not in allowed_keys:
            continue

        try:
            parsed = int(value.strip())
        except ValueError:
            continue

        values[key] = parsed

    return values


def compute_budget_plan(
    *,
    current_replicas: dict[str, int],
    target_deployment: str,
    desired_replicas: int,
    total_budget: int,
    pod_costs: dict[str, int],
    floors: dict[str, int],
    donor_priority: Iterable[str] | None = None,
) -> dict[str, Any]:
    if target_deployment not in current_replicas:
        return {
            "feasible": False,
            "reason": f"Target deployment '{target_deployment}' not found in current_replicas",
        }

    if target_deployment not in pod_costs or target_deployment not in floors:
        return {
            "feasible": False,
            "reason": f"Missing cost/floor configuration for deployment '{target_deployment}'",
        }

    if desired_replicas < floors[target_deployment]:
        return {
            "feasible": False,
            "reason": (
                f"desired_replicas={desired_replicas} is below floor for '{target_deployment}' "
                f"({floors[target_deployment]})"
            ),
        }

    for deployment in current_replicas:
        if deployment not in pod_costs or deployment not in floors:
            return {
                "feasible": False,
                "reason": f"Missing cost/floor configuration for deployment '{deployment}'",
            }

    current_usage = sum(current_replicas[name] * pod_costs[name] for name in current_replicas)
    current_target = current_replicas[target_deployment]
    delta_replicas = desired_replicas - current_target
    target_usage_without_donors = current_usage + (delta_replicas * pod_costs[target_deployment])

    plan: dict[str, Any] = {
        "feasible": False,
        "target_deployment": target_deployment,
        "desired_replicas": desired_replicas,
        "current_replicas": dict(current_replicas),
        "current_budget_usage": current_usage,
        "total_budget": total_budget,
        "headroom": total_budget - current_usage,
        "required_budget_delta": max(0, target_usage_without_donors - total_budget),
        "donor_scale_down": {},
        "proposed_replicas": dict(current_replicas),
    }

    if delta_replicas <= 0 or target_usage_without_donors <= total_budget:
        proposed = dict(current_replicas)
        proposed[target_deployment] = desired_replicas
        plan["feasible"] = True
        plan["proposed_replicas"] = proposed
        plan["resulting_budget_usage"] = sum(proposed[name] * pod_costs[name] for name in proposed)
        return plan

    needed_budget_units = target_usage_without_donors - total_budget

    donors = [name for name in current_replicas if name != target_deployment]
    if donor_priority is not None:
        preferred = [item for item in donor_priority if item in donors]
        remainder = [item for item in donors if item not in preferred]
        donors = preferred + sorted(remainder)
    else:
        donors = sorted(
            donors,
            key=lambda name: (current_replicas[name] - floors[name], current_replicas[name]),
            reverse=True,
        )

    proposed = dict(current_replicas)
    donor_scale_down: dict[str, int] = {}

    for donor in donors:
        while needed_budget_units > 0 and proposed[donor] > floors[donor]:
            proposed[donor] -= 1
            donor_scale_down[donor] = donor_scale_down.get(donor, 0) + 1
            needed_budget_units -= pod_costs[donor]

    if needed_budget_units > 0:
        plan["reason"] = (
            "Insufficient donor capacity to satisfy requested scale-up within global budget"
        )
        plan["unresolved_budget_delta"] = needed_budget_units
        return plan

    proposed[target_deployment] = desired_replicas
    resulting_usage = sum(proposed[name] * pod_costs[name] for name in proposed)
    if resulting_usage > total_budget:
        plan["reason"] = "Planned result still exceeds total budget"
        plan["resulting_budget_usage"] = resulting_usage
        return plan

    plan["feasible"] = True
    plan["donor_scale_down"] = donor_scale_down
    plan["proposed_replicas"] = proposed
    plan["resulting_budget_usage"] = resulting_usage
    return plan
