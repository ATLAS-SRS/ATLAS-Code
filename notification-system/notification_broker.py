"""
Standalone Notification Broker Service.

This service acts as a bridge for notification management:
- Consumes from score calculation pipeline
- Publishes notifications to clients
- Provides logging and monitoring
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import sys
import time
from typing import Any

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer
from health_probe import HealthServer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Health check tracking
last_consume_time = time.time()


class NotificationBroker:
    """Standalone notification broker service."""

    def __init__(self) -> None:
        """Initialize the notification broker."""
        self.broker_url = os.getenv("KAFKA_BROKER", "localhost:9092")
        self.scored_topic = os.getenv("KAFKA_SCORED_TOPIC", "scored-transactions")
        self.notification_topic = os.getenv("KAFKA_NOTIFICATION_TOPIC", "transaction-notifications")
        self.group_id = os.getenv("KAFKA_GROUP_ID", "notification_broker_group")
        self.schema_registry_url = os.getenv(
            "SCHEMA_REGISTRY_URL",
            "http://schema-registry:8081",
        )

        consumer_conf = {
            "bootstrap.servers": self.broker_url,
            "group.id": self.group_id,
            "auto.offset.reset": "earliest",
        }
        
        self.consumer = Consumer(consumer_conf)
        self.producer = Producer({"bootstrap.servers": self.broker_url})
        self.avro_deserializer = self._build_avro_deserializer()
        self.running = False

        logger.info(
            f"Notification Broker initialized. "
            f"Input={self.scored_topic} Output={self.notification_topic} "
            f"SchemaRegistry={self.schema_registry_url}"
        )

    def _build_avro_deserializer(self) -> Any | None:
        """Initialize Confluent Avro deserialization when dependencies are available."""
        try:
            from confluent_kafka.schema_registry import SchemaRegistryClient
            from confluent_kafka.schema_registry.avro import AvroDeserializer
        except ImportError as exc:
            logger.warning(
                "Avro support is unavailable because schema registry dependencies "
                "are missing: %s",
                exc,
            )
            return None

        try:
            schema_registry_client = SchemaRegistryClient(
                {"url": self.schema_registry_url}
            )
            return AvroDeserializer(schema_registry_client)
        except Exception as exc:
            logger.warning("Failed to initialize Avro deserializer: %s", exc)
            return None

    def _decode_scored_transaction(
        self,
        message: bytes | str | dict[str, Any],
    ) -> dict[str, Any]:
        """Decode a scored transaction from Avro or JSON."""
        if isinstance(message, dict):
            return dict(message)

        if isinstance(message, bytes):
            avro_payload = self._try_decode_avro(message)
            if avro_payload is not None:
                return avro_payload

            try:
                message = message.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Message is neither Avro nor UTF-8 JSON") from exc

        if isinstance(message, str):
            try:
                return json.loads(message)
            except json.JSONDecodeError as exc:
                raise ValueError("Message is not valid JSON") from exc

        raise TypeError(f"Unsupported scored transaction type: {type(message)!r}")

    def _try_decode_avro(self, message: bytes) -> dict[str, Any] | None:
        """Decode a Confluent Avro payload using the schema registry."""
        if self.avro_deserializer is None:
            return None

        try:
            from confluent_kafka.serialization import MessageField, SerializationContext

            decoded = self.avro_deserializer(
                message,
                SerializationContext(self.scored_topic, MessageField.VALUE),
            )
        except Exception as exc:
            logger.debug("Avro decode failed, falling back to JSON: %s", exc)
            return None

        if isinstance(decoded, dict):
            return decoded
        return None

    @staticmethod
    def _normalize_timestamp(timestamp: Any) -> str | None:
        """Convert timestamps from Avro logical types to the JSON format used downstream."""
        if timestamp is None:
            return None

        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        if isinstance(timestamp, (int, float)):
            dt = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
            return dt.isoformat().replace("+00:00", "Z")

        return str(timestamp)

    def _merge_payload_fields(self, scored_tx: dict[str, Any]) -> dict[str, Any]:
        """Merge the embedded JSON payload when the outer record came from Avro."""
        payload = scored_tx.get("payload")
        if not isinstance(payload, str):
            return scored_tx

        try:
            parsed_payload = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("Scored transaction payload field is not valid JSON")
            return scored_tx

        # Prefer the outer Avro envelope for canonical risk/timestamp fields while
        # preserving any additional properties from the embedded JSON payload.
        return {**parsed_payload, **scored_tx}

    def shutdown(self) -> None:
        """Backward-compatible alias used by older tests/callers."""
        self.stop()

    @staticmethod
    def delivery_report(err: KafkaError | None, msg: Any) -> None:
        """Report on message delivery status."""
        if err is not None:
            logger.error(f"Message delivery failed: {err}")
        else:
            logger.debug(
                f"Notification delivered to partition {msg.partition()} "
                f"at offset {msg.offset()}"
            )

    def run(self) -> None:
        """Start the notification broker."""
        global last_consume_time
        self.consumer.subscribe([self.scored_topic])
        self.running = True
        logger.info(f"Subscribed to topic '{self.scored_topic}'.")

        try:
            while self.running:
                last_consume_time = time.time()
                msg = self.consumer.poll(timeout=1.0)
                if msg is None:
                    continue
                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    if msg.error().code() == KafkaError.UNKNOWN_TOPIC_OR_PART:
                        continue
                    raise KafkaException(msg.error())

                payload = msg.value()
                self.process_scored_transaction(payload)
        finally:
            self.close()

    def stop(self) -> None:
        """Stop the notification broker."""
        self.running = False

    def close(self) -> None:
        """Close connections."""
        self.consumer.close()
        self.producer.flush()
        logger.info("Notification Broker closed.")

    def process_scored_transaction(self, message: bytes | str | dict[str, Any]) -> None:
        """Process a scored transaction and publish notification.
        
        Args:
            message: Avro or JSON message from scored-transactions topic
        """
        try:
            scored_tx = self._merge_payload_fields(
                self._decode_scored_transaction(message)
            )
            
            # Extract key fields
            transaction_id = scored_tx.get("transaction_id", "UNKNOWN")
            risk_score = scored_tx.get("risk_score", -1)
            risk_level = scored_tx.get("risk_level", "UNKNOWN")
            timestamp = self._normalize_timestamp(scored_tx.get("timestamp"))
            
            # Create notification payload
            notification = {
                "transaction_id": transaction_id,
                "risk_score": risk_score,
                "risk_level": risk_level,
                "status": "PROCESSED",
                "approved": risk_level == "APPROVATA",
                "timestamp": timestamp,
            }
            
            # Publish notification
            self._publish_notification(transaction_id, notification)
            
            logger.info(
                f"Transaction {transaction_id[:8]}... processed. "
                f"Score: {risk_score}, Level: {risk_level}"
            )

        except (ValueError, TypeError) as exc:
            logger.error(f"Failed to decode scored transaction: {exc}")
        except KeyError as exc:
            logger.error(f"Missing required field in scored transaction: {exc}")
        except Exception as exc:
            logger.error(f"Unexpected error processing transaction: {exc}")

    def _publish_notification(
        self,
        transaction_id: str,
        notification: dict[str, Any],
    ) -> None:
        """Publish a notification to the notification topic.
        
        Args:
            transaction_id: Transaction ID for message key
            notification: Notification payload dictionary
        """
        try:
            payload = json.dumps(notification)
            self.producer.produce(
                topic=self.notification_topic,
                key=str(transaction_id),
                value=payload,
                callback=self.delivery_report,
            )
            self.producer.poll(0)
        except Exception as exc:
            logger.error(
                f"Failed to publish notification for {transaction_id}: {exc}"
            )

    def flush(self, timeout: int = 10) -> None:
        """Flush pending messages.
        
        Args:
            timeout: Timeout in seconds
        """
        remaining = self.producer.flush(timeout)
        if remaining > 0:
            logger.warning(
                f"{remaining} notification(s) not delivered within timeout"
            )


def check_liveness() -> bool:
    """Check if the service is live based on recent Kafka activity."""
    current_time = time.time()
    consume_recent = (current_time - last_consume_time) < 30
    return consume_recent


def check_readiness() -> bool:
    """Check if the service is ready to process messages."""
    # For notification broker, readiness is always true as it's stateless
    # and resilient to connection issues. Can add more sophisticated checks if needed.
    return True


def main() -> None:
    """Main entry point."""
    broker = NotificationBroker()
    
    # Start health probe server
    health_server = HealthServer(liveness_check_fn=check_liveness, readiness_check_fn=check_readiness)
    health_server.start()
    
    try:
        logger.info("Starting Notification Broker...")
        broker.run()
    except KeyboardInterrupt:
        logger.info("Shutting down Notification Broker...")
        broker.stop()
    except Exception as exc:
        logger.critical(f"Fatal error: {exc}")
        raise


if __name__ == "__main__":
    main()
