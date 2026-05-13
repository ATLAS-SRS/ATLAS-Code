from __future__ import annotations

import json
import sys
import time
import uuid

from confluent_kafka import Consumer, Producer
from confluent_kafka import KafkaError

BROKER = "kafka:9092"
RAW_TOPIC = "raw-transactions"
ENRICHED_TOPIC = "enriched-transactions"
OUTPUT_TOPIC = "scored-transactions"

MESSAGES = [
    {
        "transaction_id": "tx-risk-1",
        "user_id": "user-1",
        "timestamp": "2026-03-23T02:30:00+00:00",
        "channel": "pos",
        "transaction_type": "payment",
        "mcc": "7995",
        "3ds_requested": False,
        "client_ip": "8.8.8.8",
        "payment_details": {
            "amount": 100.0,
            "currency": "EUR",
            "payment_method": "MAGSTRIPE_FALLBACK",
        },
    },
    {
        "transaction_id": "tx-risk-2",
        "user_id": "user-1",
        "timestamp": "2026-03-23T12:00:00+00:00",
        "channel": "pos",
        "transaction_type": "payment",
        "mcc": "5411",
        "3ds_requested": True,
        "ip_address": "8.8.8.8",
        "payment_details": {
            "amount": 100.0,
            "currency": "EUR",
            "payment_method": "credit_card",
        },
    },
]

EXPECTED = {
    "tx-risk-1": {"risk_score": 100, "risk_level": "BLOCKED"},
    "tx-risk-2": {"risk_score": 55, "risk_level": "SUSPICIOUS"},
}


def main() -> int:
    producer = Producer({"bootstrap.servers": BROKER})
    consumer = None

    try:
        for message in MESSAGES:
            producer.produce(
                RAW_TOPIC,
                key=message["transaction_id"],
                value=json.dumps(message),
            )
        producer.flush()
        time.sleep(8)

        consumer = Consumer(
            {
                "bootstrap.servers": BROKER,
                "group.id": f"scoring-integration-test-{uuid.uuid4()}",
                "auto.offset.reset": "earliest",
            }
        )
        consumer.subscribe([ENRICHED_TOPIC, OUTPUT_TOPIC])

        enriched_received: dict[str, dict[str, object]] = {}
        received: dict[str, dict[str, object]] = {}
        deadline = time.time() + 60
        while time.time() < deadline and (
            len(received) < len(EXPECTED) or len(enriched_received) < len(MESSAGES)
        ):
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() in {
                    KafkaError.UNKNOWN_TOPIC_OR_PART,
                    KafkaError._PARTITION_EOF,
                }:
                    continue
                raise RuntimeError(f"Kafka consumer error: {msg.error()}")

            payload = json.loads(msg.value().decode("utf-8"))
            tx_id = payload.get("transaction_id")
            if msg.topic() == ENRICHED_TOPIC and tx_id in EXPECTED:
                enriched_received[tx_id] = payload
            if msg.topic() == OUTPUT_TOPIC and tx_id in EXPECTED:
                received[tx_id] = payload

        missing_enriched = sorted({message["transaction_id"] for message in MESSAGES} - set(enriched_received))
        if missing_enriched:
            raise AssertionError(f"Missing enriched messages for: {missing_enriched}")

        missing = sorted(set(EXPECTED) - set(received))
        if missing:
            raise AssertionError(f"Missing scored messages for: {missing}")

        for tx_id, payload in enriched_received.items():
            if "amount" not in payload or "payment_method" not in payload:
                raise AssertionError(f"Enriched payload missing flattened fields for {tx_id}: {payload}")
            if "client_ip" not in payload:
                raise AssertionError(f"Enriched payload missing normalized client_ip for {tx_id}: {payload}")

        for tx_id, expectation in EXPECTED.items():
            payload = received[tx_id]
            for key, value in expectation.items():
                if payload.get(key) != value:
                    raise AssertionError(
                        f"Unexpected {key} for {tx_id}: expected {value}, got {payload.get(key)}"
                    )

        print("Integration test passed.")
        return 0
    except Exception as exc:
        print(f"Integration test failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if consumer is not None:
            consumer.close()


if __name__ == "__main__":
    raise SystemExit(main())
