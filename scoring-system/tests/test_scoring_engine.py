from __future__ import annotations

from datetime import UTC, datetime, timedelta

from schemas.scoring_dto import EnrichedTransactionInput, ScoredTransaction
from src.scoring_engine import RiskEvaluator


def make_transaction(**overrides):
    data = {
        "transaction_id": "tx-1",
        "user_id": "user-1",
        "amount": 100.0,
        "mcc": None,
        "payment_method": "credit_card",
        "3ds_requested": True,
        "client_ip": "127.0.0.1",
        "country_iso": "IT",
        "latitude": 41.9028,
        "longitude": 12.4964,
        "timestamp": datetime(2026, 3, 23, 12, 0, tzinfo=UTC).isoformat(),
        "channel": "pos",
    }
    data.update(overrides)
    return EnrichedTransactionInput.model_validate(data)


def test_tolerant_reader_preserves_extra_fields():
    tx = make_transaction(extra_field="kept")
    scored = ScoredTransaction(**tx.model_dump(), risk_score=10, risk_level="APPROVATA")

    assert tx.model_extra["extra_field"] == "kept"
    assert scored.model_dump()["extra_field"] == "kept"


def test_dto_accepts_ip_address_alias_for_client_ip():
    tx = EnrichedTransactionInput.model_validate(
        {
            "transaction_id": "tx-alias",
            "user_id": "user-1",
            "amount": 100.0,
            "payment_method": "credit_card",
            "timestamp": datetime(2026, 3, 23, 12, 0, tzinfo=UTC).isoformat(),
            "ip_address": "127.0.0.9",
        }
    )

    assert tx.client_ip == "127.0.0.9"


def test_dto_validates_normalized_enrichment_payload():
    payload = {
        "transaction_id": "tx-enriched",
        "user_id": 215,
        "timestamp": datetime(2026, 3, 24, 10, 0, tzinfo=UTC).isoformat(),
        "channel": "web",
        "transaction_type": "payment",
        "payment_details": {
            "amount": 149.99,
            "currency": "EUR",
            "payment_method": "credit_card",
        },
        "amount": 149.99,
        "payment_method": "credit_card",
        "currency": "EUR",
        "client_ip": "203.0.113.10",
        "country_iso": "IT",
        "latitude": 41.9028,
        "longitude": 12.4964,
    }

    tx = EnrichedTransactionInput.model_validate(payload)

    assert tx.amount == 149.99
    assert tx.payment_method == "credit_card"
    assert tx.client_ip == "203.0.113.10"
    assert tx.model_extra["payment_details"]["currency"] == "EUR"


def test_stateless_combo_and_magstripe_are_capped():
    evaluator = RiskEvaluator()
    tx = make_transaction(
        amount=200.0,
        mcc="7995",
        payment_method="MAGSTRIPE_FALLBACK",
        timestamp=datetime(2026, 3, 23, 2, 30, tzinfo=UTC).isoformat(),
        channel="pos",
    )

    result = evaluator.evaluate(tx)

    assert result.stateless_score == 105
    assert result.score == 100
    assert result.level == "FRODE"


def test_ecommerce_without_3ds_adds_risk():
    evaluator = RiskEvaluator()
    tx = make_transaction(channel="ecommerce", **{"3ds_requested": False})

    result = evaluator.evaluate(tx)

    assert result.stateless_score == 25
    assert result.level == "SOSPETTA"


def test_stateful_rules_cover_cascade_velocity_spike_and_impossible_travel():
    evaluator = RiskEvaluator()
    tx = make_transaction(
        amount=350.0,
        latitude=45.4642,
        longitude=9.19,
        timestamp=datetime(2026, 3, 23, 12, 0, tzinfo=UTC).isoformat(),
    )
    profile = {
        "last_tx_lat": 40.7128,
        "last_tx_lon": -74.0060,
        "last_tx_timestamp": (datetime(2026, 3, 23, 8, 0, tzinfo=UTC)).isoformat(),
        "last_tx_score": 90,
        "rolling_avg_amount": 100.0,
        "tx_count_10m": 6,
    }

    result = evaluator.evaluate(tx, profile)

    assert result.stateful_score == 180
    assert result.score == 100
    assert result.level == "FRODE"


def test_velocity_thresholds_are_distinct():
    evaluator = RiskEvaluator()
    tx = make_transaction(amount=10.5)

    medium = evaluator.evaluate(tx, {"tx_count_10m": 4})
    high = evaluator.evaluate(tx, {"tx_count_10m": 6})

    assert medium.stateful_score == 15
    assert high.stateful_score == 45


def test_spike_requires_positive_average_only():
    evaluator = RiskEvaluator()
    tx = make_transaction(amount=400.0)

    no_average = evaluator.evaluate(tx, {"rolling_avg_amount": 0.0})
    with_average = evaluator.evaluate(tx, {"rolling_avg_amount": 100.0})

    assert no_average.stateful_score == 0
    assert no_average.score == 5
    assert with_average.stateful_score == 25
    assert with_average.score == 30


def test_invalid_previous_timestamp_disables_impossible_travel():
    evaluator = RiskEvaluator()
    tx = make_transaction()

    result = evaluator.evaluate(
        tx,
        {
            "last_tx_lat": 10.0,
            "last_tx_lon": 10.0,
            "last_tx_timestamp": "invalid",
        },
    )

    assert result.stateful_score == 0
