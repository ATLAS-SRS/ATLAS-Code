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

from src.health_probe import HealthServer
from schemas.scoring_dto import EnrichedTransactionInput, ScoredTransaction
from src.redis_state import RedisStateClient
from src.scoring_engine import RiskEvaluator

# Health check tracking
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
            "enable.auto.commit": False
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
        self.last_consume_time = time.time()
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
                self.last_consume_time = time.time()
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
                
                try:
                    # Se process_message solleva un'eccezione, salta il commit
                    self.process_message(payload)
                    self.consumer.commit(asynchronous=False)
                except Exception as exc:
                    exc_str = str(exc)
                    # Silenzia il warning se si tratta di puro wait infrastrutturale
                    if "Connection refused" not in exc_str and "_VALUE_SERIALIZATION" not in exc_str:
                        print(f"[WARN] Elaborazione fallita. Offset non committato. Retry in corso... Dettaglio: {exc}", file=sys.stderr, flush=True)
                    time.sleep(1) # Backoff
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
            # I payload malformati vengono scartati definitivamente. Non ha senso riprovare.
            print(f"[WARN] Transazione scartata per schema non valido:\n{exc}", file=sys.stderr, flush=True)
            return

        # 1. Lettura da Redis (Hard Dependency)
        try:
            user_profile = self.redis_client.get_user_profile(incoming.user_id)
        except (redis.TimeoutError, redis.ConnectionError) as exc:
            print(f"[ERROR] Impossibile recuperare il profilo utente da Redis: {exc}", file=sys.stderr, flush=True)
            raise  # Interrompe il flusso: l'offset non verrà committato

        # 2. Calcolo dello Score
        try:
            result = self.evaluator.evaluate(
                transaction=incoming,
                user_profile=user_profile,
            )
        except Exception as exc:
            print(f"[ERROR] Errore durante il calcolo del rischio: {exc}", file=sys.stderr, flush=True)
            raise

        # 3. Aggiornamento Redis (Hard Dependency)
        try:
            self.redis_client.update_user_profile(incoming.user_id, incoming, result.score)
        except (redis.TimeoutError, redis.ConnectionError) as exc:
            print(f"[ERROR] Impossibile aggiornare il profilo su Redis: {exc}", file=sys.stderr, flush=True)
            raise

        # 4. Creazione del DTO per Avro
        scored_transaction = ScoredTransaction(
            **incoming.model_dump(),
            risk_score=result.score,
            risk_level=result.level,
        )

        full_payload_as_json_string = scored_transaction.model_dump_json(by_alias=True)
        avro_value = {
            "transaction_id": scored_transaction.transaction_id,
            "timestamp": int(scored_transaction.timestamp.timestamp() * 1000),
            "risk_score": scored_transaction.risk_score,
            "risk_level": scored_transaction.risk_level,
            "payload": full_payload_as_json_string,
        }

        # 5. Pubblicazione su Kafka
        try:
            self.producer.produce(
                topic=self.output_topic,
                key=str(incoming.transaction_id),
                value=avro_value,
                on_delivery=self.delivery_report,
            )
            self.producer.poll(0)
        except Exception as exc:
            exc_str = str(exc)
            # Identifica l'errore specifico dello Schema Registry offline
            if "Connection refused" in exc_str or "_VALUE_SERIALIZATION" in exc_str:
                print(f"[INFO] Infrastruttura non ancora pronta (Schema Registry offline). In attesa...", flush=True)
            else:
                print(f"[ERROR] Errore imprevisto durante la pubblicazione Kafka: {exc}", file=sys.stderr, flush=True)
            raise  # Rilancia l'eccezione per impedire il commit dell'offset
        
        print(
            f"[NOTIFICATION] Transazione {incoming.transaction_id} processata. "
            f"Score: {result.score}, Level: {result.level}",
            flush=True,
        )

    def check_liveness(self) -> bool:
        """Verifica se il loop di consumo è in esecuzione."""
        current_time = time.time()
        # Usa la variabile globale come hai fatto, oppure rendila attributo di classe self.last_consume_time
        return (current_time - self.last_consume_time) < 60

    def check_readiness(self) -> bool:
        """Readiness: il worker e' avviato e in loop; le dipendenze esterne possono stabilizzarsi in background."""
        return self.running

def main() -> None:
    app = ScoringSystemApp()
    
    # Istanzia e avvia health probe server agganciandolo ai metodi di classe
    health_server = HealthServer(
        port=8080, 
        liveness_check_fn=app.check_liveness, 
        readiness_check_fn=app.check_readiness
    )
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
    finally:
        health_server.stop()

if __name__ == "__main__":
    main()