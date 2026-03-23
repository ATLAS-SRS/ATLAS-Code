import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, status
from pydantic import BaseModel, Field
from aiokafka import AIOKafkaProducer
from prometheus_fastapi_instrumentator import Instrumentator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# --- 1. CONFIGURAZIONE KAFKA ---
# Leggiamo l'indirizzo dal docker-compose.yml. Se non c'è, usiamo localhost
KAFKA_BROKER = os.getenv("KAFKA_BROKER", "localhost:9092")
KAFKA_TOPIC = "raw-transactions"
producer: AIOKafkaProducer = None

# --- 2. CICLO DI VITA DELL'APP (Startup e Shutdown) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Eseguito all'avvio: connettiamo il producer a Kafka
    global producer
    producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BROKER)
    await producer.start()
    print("✅ Connessione a Kafka stabilita.")
    yield
    # Eseguito allo spegnimento: chiudiamo la connessione in modo pulito
    await producer.stop()
    print("🛑 Connessione a Kafka chiusa.")

# --- 3. CONFIGURAZIONE RATE LIMITER ---
# Usa l'IP del client per limitare le richieste (es. max 10 al minuto per IP)
limiter = Limiter(key_func=get_remote_address)

# Inizializziamo FastAPI
app = FastAPI(title="Banca Aemilia - API Gateway", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# --- 4. TELEMETRIA (Il gancio per l'Agente) ---
# Espone in automatico le metriche su /metrics (latenza, conteggio richieste, errori)
Instrumentator().instrument(app).expose(app)

# --- 5. MODELLI DATI ---
class PaymentDetails(BaseModel):
    amount: float = Field(..., gt=0)
    currency: str = Field(..., max_length=3)
    payment_method: str

class Transaction(BaseModel):
    transaction_id: str
    timestamp: str
    channel: str
    transaction_type: str
    payment_details: PaymentDetails
    model_config = {"extra": "allow"}

# --- 6. ENDPOINT PRINCIPALE ---
@app.post("/api/v1/transactions", status_code=status.HTTP_202_ACCEPTED)
#@limiter.limit("10/minute")  # Regola di affidabilità: blocca se supera le 10 richieste/min
async def ingest_transaction(request: Request, transaction: Transaction):
    try:
        # 1. Serializziamo il dato validato
        payload = transaction.model_dump_json().encode("utf-8")
        
        # 2. Pattern Event-Driven: pubblichiamo su Kafka in modo asincrono
        await producer.send_and_wait(KAFKA_TOPIC, payload)
        
        print(f"🚀 Transazione {transaction.transaction_id} inviata con successo a Kafka!")
        
        # 3. Rispondiamo al client rapidamente
        return {"status": "accepted", "transaction_id": transaction.transaction_id}
        
    except Exception as e:
        # Pattern Circuit Breaker / Graceful Degradation:
        # Se Kafka è giù, evitiamo che l'app crashi e ritorniamo 503.
        # L'Agente leggerà questo errore tramite l'endpoint /metrics.
        print(f"❌ Errore di invio a Kafka: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, 
            detail="Servizio di accodamento temporaneamente non disponibile."
        )