# evaluation_system/main.py
from __future__ import annotations

import os
import sys
import time
from typing import Any

from pydantic import ValidationError

from confluent_kafka import Producer
from src.kafka_consumer import TransactionConsumer
from src.health_probe import HealthServer
from src.fast_geoip import FastIPLocator
from schemas import TransactionInput, EnrichedTransaction


def build_enriched_payload(
    incoming_data: TransactionInput,
    geo_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base_payload = incoming_data.model_dump(by_alias=True)
    payment_details = base_payload.get("payment_details", {}) or {}
    client_ip = incoming_data.ip_address

    if client_ip is not None:
        base_payload["client_ip"] = client_ip
    base_payload.pop("ip_address", None)

    base_payload["amount"] = payment_details.get("amount")
    base_payload["payment_method"] = payment_details.get("payment_method")
    base_payload["currency"] = payment_details.get("currency")

    return {
        **base_payload,
        **(geo_data or {}),
    }

class EnrichmentSystem:
    def __init__(self):
        try:
            self.geo_locator = FastIPLocator()
            print("[INFO] GeoIP Locator inizializzato.", flush=True)
        except FileNotFoundError as e:
            print(f"[FATAL] {e}", file=sys.stderr)
            sys.exit(1)

        broker_url = os.getenv('KAFKA_BROKER', 'localhost:9092')
        topic_input = os.getenv('KAFKA_TOPIC', 'raw-transactions')
        self.enriched_topic = os.getenv('KAFKA_ENRICHED_TOPIC', 'enriched-transactions')
        group_id = os.getenv('KAFKA_GROUP_ID', 'enrichment_system_group')
        
        self.consumer = TransactionConsumer(
            broker_url=broker_url,
            group_id=group_id,
            topic=topic_input
        )

        # Inizializzazione del Producer Kafka
        self.producer = Producer({'bootstrap.servers': broker_url})
        print(f"[INFO] Kafka Producer inizializzato per il broker {broker_url}", flush=True)

        # Istanzia il server per le probe, passando i metodi di controllo
        self.health_server = HealthServer(
            port=8080,
            liveness_check_fn=self.check_liveness,
            readiness_check_fn=self.check_readiness
        )

    @staticmethod
    def delivery_report(err, msg):
        """Callback eseguita alla ricezione dell'ack da parte di Kafka."""
        if err is not None:
            print(f"[ERROR] Message delivery failed: {err}", file=sys.stderr)

    def process_transaction(self, message: str):
        """Callback eseguita per ogni messaggio da Kafka."""
        try:
            incoming_data = TransactionInput.model_validate_json(message)
            
            # 2. Localizzazione condizionale
            geo_data = {}
            if incoming_data.ip_address:
                # Eseguiamo get_geo_data solo se l'IP esiste. 
                # Usiamo 'or {}' nel caso in cui get_geo_data ritorni None (IP non trovato nel DB)
                geo_data = self.geo_locator.get_geo_data(incoming_data.ip_address) or {}
            
            # 3. Creazione del DTO di Output (Enrichment)
            # Uniamo i dati originali con la shape normalizzata attesa dallo scoring
            enriched_tx = EnrichedTransaction.model_validate(
                build_enriched_payload(incoming_data, geo_data)
            )

            # 4. Serializzazione
            final_payload = enriched_tx.model_dump_json(by_alias=True)
            print(f"[EVALUATION] Transazione arricchita: {final_payload}", flush=True)
            
            # 5. Inoltro del final_payload al topic Kafka successivo
            self.producer.produce(
                topic=self.enriched_topic,
                key=str(incoming_data.transaction_id),
                value=final_payload,
                callback=self.delivery_report
            )
            self.producer.poll(0)

        except ValidationError as e:
            # Catturiamo i messaggi malformati alla radice
            print(f"[WARN] Transazione scartata. Errore di schema:\n{e}", file=sys.stderr)
        except Exception as e:
            print(f"[ERROR] Errore imprevisto durante l'elaborazione: {e}", file=sys.stderr)

    def check_liveness(self) -> bool:
        """Controlla se il loop del consumer è attivo (heartbeat)."""
        is_alive = (time.time() - self.consumer.last_poll_timestamp) < 60
        if not is_alive:
            print(f"[ERROR] Liveness check fallito: il consumer sembra bloccato.", file=sys.stderr)
        return is_alive

    def check_readiness(self) -> bool:
        """Verifica la connettività sia del Consumer che del Producer verso Kafka."""
        # 1. Verifica Consumer
        if not self.consumer.is_connected():
            return False
            
        # 2. Verifica Producer
        if self.producer is None:
            return False
            
        try:
            # Richiede i metadati per forzare un check di rete sul socket del producer
            metadata = self.producer.list_topics(timeout=2.0)
            is_ready = metadata is not None and len(metadata.brokers) > 0
            if not is_ready:
                print("[WARN] Readiness check fallito: Producer connesso ma nessun broker nei metadati.", file=sys.stderr)
            return is_ready
        except Exception as e:
            print(f"[ERROR] Readiness check fallito lato Producer: {e}", file=sys.stderr)
            return False

if __name__ == '__main__':
    app = EnrichmentSystem()
    try:
        print("[INFO] Avvio Enrichment System...", flush=True)
        print("[INFO] Avvio del server per le health probe...", flush=True)
        app.health_server.start()
        app.consumer.start(app.process_transaction)
    except KeyboardInterrupt:
        print("\n[INFO] Arresto Enrichment System in corso...", flush=True)
        app.consumer.stop()
    finally:
        print("[INFO] Arresto del server per le health probe...", flush=True)
        app.health_server.stop()
        app.geo_locator.close()
        print("[INFO] Flush dei messaggi Kafka in sospeso...", flush=True)
        app.producer.flush()
