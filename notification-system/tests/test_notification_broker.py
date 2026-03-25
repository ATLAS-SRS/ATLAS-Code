from datetime import datetime, timezone
import json
from unittest.mock import Mock, patch

from notification_broker import NotificationBroker


def make_broker() -> NotificationBroker:
    with (
        patch("notification_broker.Consumer"),
        patch("notification_broker.Producer"),
        patch.object(NotificationBroker, "_build_avro_deserializer", return_value=None),
    ):
        broker = NotificationBroker()

    broker.producer = Mock()
    broker.producer.produce = Mock()
    return broker


def test_process_scored_transaction_accepts_json_string() -> None:
    broker = make_broker()

    broker.process_scored_transaction(
        json.dumps(
            {
                "transaction_id": "tx-json-1",
                "risk_score": 42,
                "risk_level": "APPROVATA",
                "timestamp": "2026-03-24T10:48:33Z",
            }
        )
    )

    produced = json.loads(broker.producer.produce.call_args.kwargs["value"])
    assert produced["transaction_id"] == "tx-json-1"
    assert produced["approved"] is True
    assert produced["timestamp"] == "2026-03-24T10:48:33Z"


def test_process_scored_transaction_accepts_avro_decoded_payload() -> None:
    broker = make_broker()

    broker.process_scored_transaction(
        {
            "transaction_id": "tx-avro-1",
            "risk_score": 85,
            "risk_level": "FRODE",
            "timestamp": datetime(2026, 3, 24, 10, 48, 33, tzinfo=timezone.utc),
            "payload": json.dumps(
                {
                    "transaction_id": "tx-avro-1",
                    "user_id": 123,
                    "amount": 500.0,
                    "timestamp": "2026-03-24T10:48:33Z",
                }
            ),
        }
    )

    produced = json.loads(broker.producer.produce.call_args.kwargs["value"])
    assert produced["transaction_id"] == "tx-avro-1"
    assert produced["risk_level"] == "FRODE"
    assert produced["approved"] is False
    assert produced["timestamp"] == "2026-03-24T10:48:33Z"
