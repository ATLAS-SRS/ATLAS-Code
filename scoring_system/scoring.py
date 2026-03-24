from __future__ import annotations

import os
import sys
from typing import Any

import redis
from confluent_kafka import Consumer, KafkaError, KafkaException, Producer
from pydantic import ValidationError

from scoring_system.schemas.scoring_dto import EnrichedTransactionInput, ScoredTransaction
from scoring_system.src.redis_state import RedisStateClient
from scoring_system.src.scoring_engine import RiskEvaluator


class ScoringSystemApp:
    def __init__(self) -> None:
        broker_url = os.getenv("KAFKA_BROKER", "localhost:9092")
        group_id = os.getenv("KAFKA_GROUP_ID", "scoring_system_group")
        self.input_topic = os.getenv("KAFKA_ENRICHED_TOPIC", "enriched-transactions")
        self.output_topic = os.getenv("KAFKA_SCORED_TOPIC", "scored-transactions")

        consumer_conf = {
            "bootstrap.servers": broker_url,
            "group.id": group_id,
            "auto.offset.reset": "earliest",
        }
        self.consumer = Consumer(consumer_conf)
        self.producer = Producer({"bootstrap.servers": broker_url})
        self.evaluator = RiskEvaluator()
        self.running = False
        self.redis_client = RedisStateClient(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
            socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT", "1.0")),
            profile_ttl_seconds=int(os.getenv("REDIS_PROFILE_TTL_SECONDS", "86400")),
        )
        print(
            f"[INFO] Scoring system inizializzato. Input={self.input_topic} Output={self.output_topic}",
            flush=True,
        )

    @staticmethod
    def delivery_report(err: KafkaError | None, msg: Any) -> None:
        if err is not None:
            print(f"[ERROR] Message delivery failed: {err}", file=sys.stderr, flush=True)

    def run(self) -> None:
        self.consumer.subscribe([self.input_topic])
        self.running = True
        print(f"[INFO] Sottoscritto al topic '{self.input_topic}'.", flush=True)

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
                self.process_message(payload)
        finally:
            self.close()

    def stop(self) -> None:
        self.running = False

    def close(self) -> None:
        self.consumer.close()
        self.producer.flush()
        print("[INFO] Consumer chiuso e producer flushato.", flush=True)

    def process_message(self, message: str) -> None:
        try:
            incoming = EnrichedTransactionInput.model_validate_json(message)
        except ValidationError as exc:
            print(f"[WARN] Transazione scartata per schema non valido:\n{exc}", file=sys.stderr, flush=True)
            return

        user_profile: dict[str, Any] | None = None
        redis_available = True
        try:
            user_profile = self.redis_client.get_user_profile(incoming.user_id)
        except (redis.TimeoutError, redis.ConnectionError) as exc:
            redis_available = False
            print(
                f"[WARN] Redis temporaneamente non disponibile in lettura, applico solo regole stateless: {exc}",
                file=sys.stderr,
                flush=True,
            )

        try:
            result = self.evaluator.evaluate(
                transaction=incoming,
                user_profile=user_profile if redis_available else None,
            )
        except Exception as exc:
            print(f"[ERROR] Errore durante il calcolo del rischio: {exc}", file=sys.stderr, flush=True)
            return

        if redis_available:
            try:
                self.redis_client.update_user_profile(incoming.user_id, incoming, result.score)
            except (redis.TimeoutError, redis.ConnectionError) as exc:
                print(
                    f"[WARN] Redis temporaneamente non disponibile in scrittura, stato non aggiornato: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

        scored_transaction = ScoredTransaction(
            **incoming.model_dump(),
            risk_score=result.score,
            risk_level=result.level,
        )

        try:
            self.producer.produce(
                topic=self.output_topic,
                key=str(incoming.transaction_id),
                value=scored_transaction.model_dump_json(by_alias=True),
                callback=self.delivery_report,
            )
            self.producer.poll(0)
        except Exception as exc:
            print(f"[ERROR] Errore durante la pubblicazione Kafka: {exc}", file=sys.stderr, flush=True)


def main() -> None:
    app = ScoringSystemApp()
    try:
        print("[INFO] Avvio Scoring System...", flush=True)
        app.run()
    except KeyboardInterrupt:
        print("\n[INFO] Arresto Scoring System in corso...", flush=True)
        app.stop()
    except Exception as exc:
        print(f"[FATAL] Arresto anomalo dello scoring system: {exc}", file=sys.stderr, flush=True)
        raise


if __name__ == "__main__":
    main()
