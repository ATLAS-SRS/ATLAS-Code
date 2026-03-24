from __future__ import annotations

import json
from datetime import UTC, datetime

from enrichment_system import EvaluationSystem, build_enriched_payload
from schemas import EnrichedTransaction, TransactionInput


class FakeProducer:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    def produce(self, **kwargs) -> None:
        self.messages.append(kwargs)

    def poll(self, timeout: float) -> None:
        return None


class FakeGeoLocator:
    def __init__(self, response: dict[str, object] | None) -> None:
        self.response = response
        self.calls: list[str] = []

    def get_geo_data(self, ip_address: str) -> dict[str, object] | None:
        self.calls.append(ip_address)
        return self.response


def make_raw_payload(**overrides) -> dict[str, object]:
    payload = {
        "transaction_id": "tx-enrichment-1",
        "timestamp": datetime(2026, 3, 24, 10, 0, tzinfo=UTC).isoformat(),
        "channel": "web",
        "transaction_type": "payment",
        "user_id": 215,
        "payment_details": {
            "amount": 149.99,
            "currency": "EUR",
            "payment_method": "credit_card",
        },
        "client_ip": "203.0.113.10",
        "merchant_name": "Demo Shop",
    }
    payload.update(overrides)
    return payload


def make_app(geo_response: dict[str, object] | None) -> tuple[EvaluationSystem, FakeProducer, FakeGeoLocator]:
    app = EvaluationSystem.__new__(EvaluationSystem)
    producer = FakeProducer()
    locator = FakeGeoLocator(geo_response)
    app.producer = producer
    app.geo_locator = locator
    app.enriched_topic = "enriched-transactions"
    return app, producer, locator


def test_build_enriched_payload_flattens_gateway_shape_and_preserves_extra_fields():
    incoming = TransactionInput.model_validate(make_raw_payload())

    payload = build_enriched_payload(
        incoming,
        {"country_iso": "IT", "latitude": 41.9, "longitude": 12.5},
    )
    enriched = EnrichedTransaction.model_validate(payload)

    assert enriched.amount == 149.99
    assert enriched.payment_method == "credit_card"
    assert enriched.currency == "EUR"
    assert enriched.client_ip == "203.0.113.10"
    assert enriched.user_id == 215
    assert enriched.model_extra["merchant_name"] == "Demo Shop"


def test_process_transaction_uses_client_ip_for_geo_lookup_and_preserves_it():
    app, producer, locator = make_app(
        {"country_iso": "IT", "city_name": "Rome", "latitude": 41.9, "longitude": 12.5}
    )

    app.process_transaction(json.dumps(make_raw_payload()))

    assert locator.calls == ["203.0.113.10"]
    assert len(producer.messages) == 1
    published = json.loads(producer.messages[0]["value"])
    assert published["client_ip"] == "203.0.113.10"
    assert published["amount"] == 149.99
    assert published["payment_method"] == "credit_card"
    assert published["country_iso"] == "IT"


def test_process_transaction_accepts_ip_address_and_normalizes_to_client_ip():
    app, producer, locator = make_app({"country_iso": "US"})
    raw_payload = make_raw_payload(client_ip=None, ip_address="198.51.100.7")

    app.process_transaction(json.dumps(raw_payload))

    assert locator.calls == ["198.51.100.7"]
    published = json.loads(producer.messages[0]["value"])
    assert published["client_ip"] == "198.51.100.7"
    assert "ip_address" not in published
