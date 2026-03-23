import json
import os
from confluent_kafka import Consumer, KafkaError, KafkaException

def elabora_transazione(messaggio):
    """
    Qui inseriremo la logica per chiamare il DB e verificare lo storico.
    Per ora, ci limitiamo a stampare il contenuto.
    """
    try:
        # Supponiamo che il messaggio sia in formato JSON
        transazione = json.loads(messaggio)
        print(f"Transazione ricevuta: {transazione}")
    except json.JSONDecodeError:
        print(f"Errore di decodifica JSON. Messaggio raw: {messaggio}")

def extract_transactions_from_kafka():
    # Configurazione del Consumer
    bootstrap_servers = os.getenv('KAFKA_BROKER', 'localhost:9092')
    conf = {
        'bootstrap.servers': bootstrap_servers, 
        'group.id': 'transaction_group_1a',
        'auto.offset.reset': 'earliest'
    }

    consumer = Consumer(conf)
    topic = 'raw-transactions' # Il nome del tuo topic Kafka

    try:
        consumer.subscribe([topic])
        print(f"[DEBUG] Sottoscrizione al topic '{topic}' completata.", flush=True)

        while True:
            # Poll per nuovi messaggi (timeout di 1 secondo)
            msg = consumer.poll(timeout=1.0)

            if msg is None:
                continue

            if msg.error():
                print(f"Errore nel messaggio: {msg.error()}")
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    # Fine della partizione, non è un vero errore
                    continue
                elif msg.error().code() == KafkaError.UNKNOWN_TOPIC_OR_PART:
                    # Il topic non esiste ancora, ignora e riprova al prossimo ciclo
                    continue
                elif msg.error():
                    raise KafkaException(msg.error())
            else:
                print(f"Messaggio ricevuto: {msg.value().decode('utf-8')}")
                # Messaggio letto correttamente
                valore_msg = msg.value().decode('utf-8')
                elabora_transazione(valore_msg)

    except KeyboardInterrupt:
        print("Interruzione manuale da tastiera.")
    finally:
        # Chiude il consumer in modo pulito per rilasciare il gruppo
        consumer.close()
        print("Consumer chiuso.")

if __name__ == '__main__':
    extract_transactions_from_kafka()