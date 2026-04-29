from src.agent_guardian.budgeting import compute_budget_plan, parse_keyed_ints


def test_parse_keyed_ints_overrides_only_allowed_keys() -> None:
    parsed = parse_keyed_ints(
        "api-gateway=2,scoring-system=3,unknown=99,broken,no-equals",
        {"api-gateway", "scoring-system", "notification-system"},
        default_value=1,
    )

    assert parsed == {
        "api-gateway": 2,
        "scoring-system": 3,
        "notification-system": 1,
    }


def test_compute_budget_plan_scales_target_without_donors_when_headroom_exists() -> None:
    plan = compute_budget_plan(
        current_replicas={
            "api-gateway": 2,
            "scoring-system": 1,
            "enrichment-system": 1,
            "notification-system": 1,
        },
        target_deployment="api-gateway",
        desired_replicas=3,
        total_budget=8,
        pod_costs={
            "api-gateway": 1,
            "scoring-system": 1,
            "enrichment-system": 1,
            "notification-system": 1,
        },
        floors={
            "api-gateway": 1,
            "scoring-system": 1,
            "enrichment-system": 1,
            "notification-system": 1,
        },
    )

    assert plan["feasible"] is True
    assert plan["donor_scale_down"] == {}
    assert plan["proposed_replicas"]["api-gateway"] == 3


def test_compute_budget_plan_borrows_from_donors_with_priority() -> None:
    plan = compute_budget_plan(
        current_replicas={
            "api-gateway": 3,
            "scoring-system": 2,
            "enrichment-system": 2,
            "notification-system": 1,
        },
        target_deployment="api-gateway",
        desired_replicas=5,
        total_budget=8,
        pod_costs={
            "api-gateway": 1,
            "scoring-system": 1,
            "enrichment-system": 1,
            "notification-system": 1,
        },
        floors={
            "api-gateway": 1,
            "scoring-system": 1,
            "enrichment-system": 1,
            "notification-system": 1,
        },
        donor_priority=["scoring-system", "enrichment-system"],
    )

    assert plan["feasible"] is True
    assert plan["donor_scale_down"] == {"scoring-system": 1, "enrichment-system": 1}
    assert sum(plan["proposed_replicas"].values()) <= plan["total_budget"]
