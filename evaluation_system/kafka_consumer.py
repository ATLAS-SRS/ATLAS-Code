import json
import os
import sys
import argparse
from confluent_kafka import Consumer, KafkaError, KafkaException

class TransactionConsumer:
    def __init__(self, broker_url, group_id, topic, auto_offset='earliest'):
        """
        Inizializza il consumer Kafka. Nessun hardcoding dei parametri operativi.
        """
        conf = {
            'bootstrap.servers': broker_url, 
            'group.id': group_id,
            'auto.offset.reset': auto_offset
        }
        self.topic = topic
        self.consumer = Consumer(conf)
        self.running = False

    def start(self, callback_func):
        """
        Avvia il ciclo di consumo. 
        Riceve una funzione (callback_func) che definisce COSA fare con il messaggio.
        """
        self.consumer.subscribe([self.topic])
        self.running = True
        print(f"[DEBUG] Sottoscrizione al topic '{self.topic}' completata.", flush=True)

        try:
            while self.running:
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
        finally:
            self.close()

    def stop(self):
        """Segnala al loop di fermarsi."""
        self.running = False

    def close(self):
        """Rilascia le risorse del consumer."""
        self.consumer.close()
        print("[DEBUG] Consumer chiuso correttamente.")

# --- SEZIONE STANDALONE ---

def elabora_transazione_default(messaggio):
    """Logica di default usata quando lo script è eseguito direttamente."""
    try:
        transazione = json.loads(messaggio)
        print(f"Transazione ricevuta: {transazione}")
    except json.JSONDecodeError:
        print(f"Errore di decodifica JSON. Messaggio raw: {messaggio}", file=sys.stderr)

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
        print("\nInterruzione manuale da tastiera.")
        app.stop()

if __name__ == '__main__':
    main()