import time
import random
import requests
import os
import uuid
from datetime import datetime, timezone # Aggiunto per il timestamp

# Legge l'URL del gateway dall'ambiente di Docker
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8000/api/v1/transactions")
SLEEP_TIME = float(os.getenv("SLEEP_TIME", 2.0))


def generate_ip_address():
    """Genera un IPv4 pubblico semplice evitando i range privati più comuni."""
    first_octet = random.choice([8, 23, 45, 52, 63, 80, 91, 101, 121, 151, 185, 203])
    return ".".join(
        [
            str(first_octet),
            str(random.randint(1, 254)),
            str(random.randint(1, 254)),
            str(random.randint(1, 254)),
        ]
    )

def generate_transaction():
    """Genera un payload JSON allineato al modello del Gateway."""
    return {
        "transaction_id": str(uuid.uuid4()),
        # Genera un timestamp in formato ISO 8601 (es. 2026-03-18T15:30:00Z)
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "channel": random.choice(["web", "mobile", "app", "pos"]),
        "transaction_type": random.choice(["payment", "refund", "subscription"]),
        
        # Ecco il dizionario annidato che il gateway pretende!
        "payment_details": {
            "amount": round(random.uniform(5.0, 500.0), 2),
            "currency": random.choice(["EUR", "USD", "GBP"]),
            "payment_method": random.choice(["credit_card", "paypal", "apple_pay"])
        },
        "ip_address": generate_ip_address(),
        # Visto che c'è "extra: allow", possiamo mandare campi extra senza far crashare il gateway
        "user_id": random.randint(100, 999)
    }

def main():
    print(f"🚀 Avvio del client Python. Invio traffico a: {GATEWAY_URL}")
    
    while True:
        payload = generate_transaction()
        try:
            # Aggiungiamo timeout per evitare che il client si blocchi se il gateway è lento
            response = requests.post(GATEWAY_URL, json=payload, timeout=5)
            
            # Stampiamo il risultato. Il gateway del collega restituisce 202 ACCEPTED
            if response.status_code == 202:
                print(f"✅ Inviato ID {payload['transaction_id'][:8]}... | Status: 202 ACCEPTED")
            else:
                print(f"⚠️ Inviato ID {payload['transaction_id'][:8]}... | Status inatteso: {response.status_code} - {response.text}")
                
        except requests.exceptions.RequestException as e:
            print(f"❌ Errore di connessione al gateway: {e}")
        
        time.sleep(SLEEP_TIME)

if __name__ == "__main__":
    main()
