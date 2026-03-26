from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import redis
from confluent_kafka import Consumer, KafkaError, KafkaException, SerializingProducer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroSerializer
from confluent_kafka.serialization import StringSerializer
from pydantic import ValidationError

from scoring_system.src.health_probe import HealthServer
from scoring_system.schemas.scoring_dto import EnrichedTransactionInput, ScoredTransaction
from scoring_system.src.redis_state import RedisStateClient
from scoring_system.src.scoring_engine import RiskEvaluator

# Health check tracking
last_consume_time = time.time()
redis_client_global = None


class ScoringSystemApp:
    def __init__(self) -> None:
        # --- Configurazione Connessioni ---
        broker_url = os.getenv("KAFKA_BROKER", "localhost:9092")
        group_id = os.getenv("KAFKA_GROUP_ID", "scoring_system_group")
        self.input_topic = os.getenv("KAFKA_ENRICHED_TOPIC", "enriched-transactions")
        self.output_topic = os.getenv("KAFKA_SCORED_TOPIC", "scored-transactions")
        schema_registry_url = os.getenv("SCHEMA_REGISTRY_URL", "http://schema-registry:8081")

        # --- Configurazione Consumer (legge JSON dal topic di input) ---
        consumer_conf = {
            "bootstrap.servers": broker_url,
            "group.id": group_id,
            "auto.offset.reset": "earliest",
        }
        self.consumer = Consumer(consumer_conf)

        # --- Configurazione Producer (scrive AVRO sul topic di output) ---
        schema_registry_conf = {"url": schema_registry_url}
        schema_registry_client = SchemaRegistryClient(schema_registry_conf)

        # Serializzatore per la chiave (semplice stringa UTF-8)
        key_serializer = StringSerializer("utf_8")

        # Schema Avro per il valore. Include i campi necessari per il DB.
        value_schema_str = """
        {
           "namespace": "com.atlas.transactions",
           "name": "ScoredTransactionAvro",
           "type": "record",
           "fields" : [
             { "name" : "transaction_id", "type" : "string" },
             { "name" : "timestamp", "type" : { "type": "long", "logicalType": "timestamp-millis" } },
             { "name" : "risk_score", "type" : "int" },
             { "name" : "risk_level", "type" : "string" },
             { "name" : "payload", "type" : "string" }
           ]
        }
        """
        value_serializer = AvroSerializer(
            schema_registry_client, value_schema_str,
        )

        producer_conf = {
            "bootstrap.servers": broker_url,
            "key.serializer": key_serializer,
            "value.serializer": value_serializer,
        }
        self.producer = SerializingProducer(producer_conf)

        # --- Configurazione Componenti Logici ---
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
        global last_consume_time
        self.consumer.subscribe([self.input_topic])
        self.running = True
        print(f"[INFO] Sottoscritto al topic '{self.input_topic}'.", flush=True)

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

                payload = msg.value().decode("utf-8")
                self.process_message(payload)
        finally:
            self.close()

    def stop(self) -> None:
        self.running = False

    def close(self) -> None:
        self.consumer.close()
        self.producer.flush()
        print("[INFO] Consumer chiuso, producers flushati.", flush=True)

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

        # Crea il DTO finale
        scored_transaction = ScoredTransaction(
            **incoming.model_dump(),
            risk_score=result.score,
            risk_level=result.level,
        )

        # 1. Serializziamo l'intero oggetto `ScoredTransaction` in una stringa JSON.
        #    Questo sarà il contenuto del campo 'payload' del nostro record Avro.
        full_payload_as_json_string = scored_transaction.model_dump_json(by_alias=True)

        # 2. Creiamo il dizionario che corrisponde allo schema Avro del valore.
        avro_value = {
            "transaction_id": scored_transaction.transaction_id,
            "timestamp": int(scored_transaction.timestamp.timestamp() * 1000),
            "risk_score": scored_transaction.risk_score,
            "risk_level": scored_transaction.risk_level,
            "payload": full_payload_as_json_string,
        }

        print(f"[INFO] Transazione valutata: ID={incoming.transaction_id} Score={result.score} Level={result.level}", flush=True)

        try:
            self.producer.produce(
                topic=self.output_topic,
                key=str(incoming.transaction_id),
                value=avro_value,
                on_delivery=self.delivery_report,
            )
            self.producer.poll(0)
        except Exception as exc:
            print(f"[ERROR] Errore durante la pubblicazione Kafka: {exc}", file=sys.stderr, flush=True)
            return
        
        print(
            f"[NOTIFICATION] Transazione {incoming.transaction_id} processata. "
            f"Score: {result.score}, Level: {result.level}",
            flush=True,
        )


def check_liveness() -> bool:
    """Check if the service is live based on recent Kafka activity."""
    current_time = time.time()
    consume_recent = (current_time - last_consume_time) < 30
    return consume_recent


def check_readiness() -> bool:
    """Check if the service is ready (Redis available)."""
    if redis_client_global is None:
        return False
    try:
        redis_client_global.redis_conn.ping()
        return True
    except Exception:
        return False


def main() -> None:
    global redis_client_global
    app = ScoringSystemApp()
    redis_client_global = app.redis_client
    
    # Start health probe server
    health_server = HealthServer(liveness_check_fn=check_liveness, readiness_check_fn=check_readiness)
    health_server.start()
    
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
