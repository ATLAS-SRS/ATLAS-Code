import json
import os
import sys
import argparse
import time
from confluent_kafka import Consumer, KafkaError, KafkaException
from structured_logger import get_logger


logger = get_logger("enrichment-system-consumer")

class TransactionConsumer:
    def __init__(self, broker_url, group_id, topic, auto_offset='earliest', session_timeout_ms=45000, max_poll_interval_ms=300000, heartbeat_interval_ms=3000):
        """
        Inizializza il consumer Kafka. Nessun hardcoding dei parametri operativi.
        """
        conf = {
            'bootstrap.servers': broker_url, 
            'group.id': group_id,
            'auto.offset.reset': auto_offset,
            'enable.auto.commit': False,
            'session.timeout.ms': session_timeout_ms,
            'max.poll.interval.ms': max_poll_interval_ms,
            'heartbeat.interval.ms': heartbeat_interval_ms
        }

        self.topic = topic
        self.consumer = Consumer(conf)
        self.running = False
        self.last_poll_timestamp = time.time()

    def start(self, callback_func):
        """
        Avvia il ciclo di consumo. 
        Riceve una funzione (callback_func) che definisce COSA fare con il messaggio.
        """
        self.consumer.subscribe([self.topic])
        self.running = True
        logger.info("Subscribed to Kafka topic", extra={"topic": self.topic})

        try:
            while self.running:
                self.last_poll_timestamp = time.time()
                msg = self.consumer.poll(timeout=1.0)

                if msg is None:
                    continue

                if msg.error():
                    # Gestione errori Kafka
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    elif msg.error().code() == KafkaError.UNKNOWN_TOPIC_OR_PART:
                        continue
                    else:
                        raise KafkaException(msg.error())
                else:
                    # Delega l'elaborazione del messaggio alla funzione esterna
                    valore_msg = msg.value().decode('utf-8')
                    callback_func(valore_msg)
                    self.consumer.commit(asynchronous=False)
        finally:
            self.close()

    def stop(self):
        """Segnala al loop di fermarsi."""
        self.running = False

    def close(self):
        """Rilascia le risorse del consumer."""
        self.consumer.close()
        logger.info("Kafka consumer closed")

    def is_connected(self) -> bool:
        """Verifica se il consumer è in grado di comunicare con il broker Kafka."""
        try:
            metadata = self.consumer.list_topics(timeout=2.0)
            return metadata is not None and len(metadata.brokers) > 0
        except KafkaException as e:
            logger.error("Consumer readiness check failed", extra={"error": str(e)})
            return False

# --- SEZIONE STANDALONE ---

def elabora_transazione_default(messaggio):
    """Logica di default usata quando lo script è eseguito direttamente."""
    try:
        transazione = json.loads(messaggio)
        logger.info("Transaction received", extra={"transaction": transazione})
    except json.JSONDecodeError:
        logger.error("Failed to decode JSON message", extra={"raw_message": messaggio})

def main():
    parser = argparse.ArgumentParser(description="Kafka Transaction Consumer")
    # L'ambiente è il fallback, ma la CLI ha la precedenza
    parser.add_argument("--broker", default=os.getenv('KAFKA_BROKER', 'localhost:9092'), help="Indirizzo Kafka broker")
    parser.add_argument("--topic", default="raw-transactions", help="Nome del topic")
    parser.add_argument("--group", default="transaction_group_1a", help="Consumer group ID")
    
    args = parser.parse_args()

    app = TransactionConsumer(args.broker, args.group, args.topic)
    
    try:
        # Passiamo la logica di default al loop
        app.start(elabora_transazione_default)
    except KeyboardInterrupt:
        logger.info("Manual keyboard interruption received")
        app.stop()

if __name__ == '__main__':
    main()