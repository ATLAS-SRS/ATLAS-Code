from fastapi import FastAPI, status
from pydantic import BaseModel
from typing import Dict, Any

# --- 1. DEFINIZIONE DEI MODELLI ---
# Definiamo la struttura base per testare la ricezione
class PaymentDetails(BaseModel):
    amount: float
    currency: str
    payment_method: str

class Transaction(BaseModel):
    transaction_id: str
    timestamp: str
    channel: str
    transaction_type: str
    payment_details: PaymentDetails
    
    # Questo permette al modello di accettare campi extra (come customer e merchant)
    # senza dare errore in questa fase di test
    model_config = {
        "extra": "allow"
    }

# --- 2. INIZIALIZZAZIONE DELL'APP ---
app = FastAPI(title="Gateway Fantoccio - Banca Aemilia")

# --- 3. ENDPOINT DI RICEZIONE ---
@app.post("/api/v1/transactions", status_code=status.HTTP_202_ACCEPTED)
async def ingest_transaction(transaction: Transaction):
    # Formattiamo e stampiamo il JSON ricevuto nel terminale
    print("\n" + "="*40)
    print("🚨 NUOVA TRANSAZIONE RICEVUTA 🚨")
    print("="*40)
    
    # model_dump_json(indent=2) formatta il JSON in modo leggibile
    print(transaction.model_dump_json(indent=2))
    
    print("="*40 + "\n")
    
    # Rispondiamo al client
    return {
        "status": "accepted", 
        "transaction_id": transaction.transaction_id,
        "message": "Transazione ricevuta e stampata sul terminale del server."
    }