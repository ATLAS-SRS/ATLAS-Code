# evaluation_system/main.py
from __future__ import annotations

import os
import sys
from typing import Any

from pydantic import ValidationError

from confluent_kafka import Producer
from src.kafka_consumer import TransactionConsumer
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

class EvaluationSystem:
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
        group_id = os.getenv('KAFKA_GROUP_ID', 'evaluation_system_group')
        
        self.consumer = TransactionConsumer(
            broker_url=broker_url,
            group_id=group_id,
            topic=topic_input
        )

        # Inizializzazione del Producer Kafka
        self.producer = Producer({'bootstrap.servers': broker_url})
        print(f"[INFO] Kafka Producer inizializzato per il broker {broker_url}", flush=True)

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

if __name__ == '__main__':
    app = EvaluationSystem()
    try:
        print("[INFO] Avvio Evaluation System...", flush=True)
        app.consumer.start(app.process_transaction)
    except KeyboardInterrupt:
        print("\n[INFO] Arresto Evaluation System in corso...", flush=True)
        app.consumer.stop()
    finally:
        app.geo_locator.close()
        print("[INFO] Flush dei messaggi Kafka in sospeso...", flush=True)
        app.producer.flush()
