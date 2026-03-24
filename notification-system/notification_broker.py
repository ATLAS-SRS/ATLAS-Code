"""
Standalone Notification Broker Service.

This service acts as a bridge for notification management:
- Consumes from score calculation pipeline
- Publishes notifications to clients
- Provides logging and monitoring
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)


class NotificationBroker:
    """Standalone notification broker service."""

    def __init__(self) -> None:
        """Initialize the notification broker."""
        self.broker_url = os.getenv("KAFKA_BROKER", "localhost:9092")
        self.scored_topic = os.getenv("KAFKA_SCORED_TOPIC", "scored-transactions")
        self.notification_topic = os.getenv("KAFKA_NOTIFICATION_TOPIC", "transaction-notifications")
        self.group_id = os.getenv("KAFKA_GROUP_ID", "notification_broker_group")

        consumer_conf = {
            "bootstrap.servers": self.broker_url,
            "group.id": self.group_id,
            "auto.offset.reset": "earliest",
        }
        
        self.consumer = Consumer(consumer_conf)
        self.producer = Producer({"bootstrap.servers": self.broker_url})
        self.running = False

        logger.info(
            f"Notification Broker initialized. "
            f"Input={self.scored_topic} Output={self.notification_topic}"
        )

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
        self.consumer.subscribe([self.scored_topic])
        self.running = True
        logger.info(f"Subscribed to topic '{self.scored_topic}'.")

        try:
            while self.running:
                msg = self.consumer.poll(timeout=1.0)
                if msg is None:
                    continue
                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    if msg.error().code() == KafkaError.UNKNOWN_TOPIC_OR_PART:
                        continue
                    raise KafkaException(msg.error())

                payload = msg.value().decode("utf-8")
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

    def process_scored_transaction(self, message: str) -> None:
        """Process a scored transaction and publish notification.
        
        Args:
            message: JSON message from scored-transactions topic
        """
        try:
            scored_tx = json.loads(message)
            
            # Extract key fields
            transaction_id = scored_tx.get("transaction_id", "UNKNOWN")
            risk_score = scored_tx.get("risk_score", -1)
            risk_level = scored_tx.get("risk_level", "UNKNOWN")
            
            # Create notification payload
            notification = {
                "transaction_id": transaction_id,
                "risk_score": risk_score,
                "risk_level": risk_level,
                "status": "PROCESSED",
                "approved": risk_level == "APPROVATA",
                "timestamp": scored_tx.get("timestamp"),
            }
            
            # Publish notification
            self._publish_notification(transaction_id, notification)
            
            logger.info(
                f"Transaction {transaction_id[:8]}... processed. "
                f"Score: {risk_score}, Level: {risk_level}"
            )

        except json.JSONDecodeError as exc:
            logger.error(f"Failed to parse scored transaction: {exc}")
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


def main() -> None:
    """Main entry point."""
    broker = NotificationBroker()
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
