import os
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, status
from pydantic import BaseModel, Field
from aiokafka import AIOKafkaProducer
from prometheus_fastapi_instrumentator import Instrumentator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from structured_logger import get_logger

# --- 1. KAFKA CONFIGURATION ---
# Read the address from docker-compose.yml. If it is missing, use localhost.
KAFKA_BROKER = os.getenv("KAFKA_BROKER", "localhost:9092")
KAFKA_TOPIC = "raw-transactions"
producer: AIOKafkaProducer = None
kafka_connected: bool = False
connection_task: asyncio.Task = None
logger = get_logger("gateway")

# Exponential backoff configuration for Kafka connection retries
MIN_RETRY_DELAY = 2  # Initial delay in seconds
MAX_RETRY_DELAY = 32  # Maximum delay in seconds (caps exponential growth)

async def _kafka_connection_task():
    """
    Background task to establish Kafka connection with exponential backoff.
    Never raises exceptions - the application continues running regardless.
    Kubernetes liveness probe will see the HTTP server running and won't restart.
    Kubernetes readiness probe will hold traffic until Kafka is connected.
    """
    global kafka_connected
    retry_attempt = 0
    retry_delay = MIN_RETRY_DELAY
    
    try:
        while not kafka_connected:
            retry_attempt += 1
            try:
                logger.info(
                    "Attempting to connect to Kafka broker",
                    extra={
                        "kafka_broker": KAFKA_BROKER,
                        "attempt": retry_attempt,
                        "phase": "kafka_connection",
                    },
                )
                
                await producer.start()
                kafka_connected = True
                
                logger.info(
                    "Successfully connected to Kafka broker",
                    extra={
                        "kafka_broker": KAFKA_BROKER,
                        "total_attempts": retry_attempt,
                        "phase": "kafka_connection",
                    },
                )
                break
                
            except Exception as e:
                logger.warning(
                    "Waiting for Kafka to become available",
                    extra={
                        "kafka_broker": KAFKA_BROKER,
                        "attempt": retry_attempt,
                        "retry_delay_seconds": retry_delay,
                        "error_type": type(e).__name__,
                        "error_message": str(e),
                        "phase": "kafka_connection",
                    },
                )
                
                await asyncio.sleep(retry_delay)
                
                # Exponential backoff: 2s → 4s → 8s → 16s → 32s → 32s → ...
                retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)
                
    except asyncio.CancelledError:
        logger.warning(
            "Kafka connection task cancelled during shutdown",
            extra={"phase": "kafka_connection", "attempt": retry_attempt},
        )

# --- 2. CICLO DI VITA DELL'APP (Startup e Shutdown) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler implementing graceful degradation pattern.
    
    STARTUP:
    - HTTP server starts immediately (does not wait for Kafka)
    - Kafka connection attempt happens in background with exponential backoff
    - Liveness probe responds 200 OK immediately
    - Readiness probe responds 503 until Kafka is ready
    
    SHUTDOWN:
    - Gracefully cancels background Kafka connection task
    - Closes Kafka producer if connected
    """
    global producer, connection_task
    
    logger.info(
        "API Gateway starting up",
        extra={
            "kafka_broker": KAFKA_BROKER,
            "phase": "startup",
            "startup_phase": "initialization",
        },
    )
    
    # Create Kafka producer (does not connect yet, just initializes the object)
    producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BROKER)
    
    # Spawn background task to establish Kafka connection with retries
    connection_task = asyncio.create_task(_kafka_connection_task())
    
    logger.info(
        "HTTP server ready to accept requests",
        extra={
            "phase": "startup",
            "startup_phase": "http_ready",
            "kafka_status": "connecting_in_background",
        },
    )
    
    yield  # Application is now running
    
    # --- SHUTDOWN PHASE ---
    logger.info(
        "API Gateway shutting down",
        extra={"phase": "shutdown", "shutdown_phase": "initiated"},
    )
    
    # Cancel background Kafka connection task
    if connection_task and not connection_task.done():
        connection_task.cancel()
        try:
            await connection_task
        except asyncio.CancelledError:
            logger.info(
                "Kafka connection task cancelled during shutdown",
                extra={"phase": "shutdown", "shutdown_phase": "task_cancelled"},
            )
    
    # Close Kafka producer if it was successfully connected
    if kafka_connected:
        try:
            await producer.stop()
            logger.info(
                "Kafka connection closed",
                extra={"phase": "shutdown", "shutdown_phase": "kafka_closed"},
            )
        except Exception as e:
            logger.error(
                "Error closing Kafka connection",
                extra={"phase": "shutdown", "error": str(e)},
            )
    
    logger.info(
        "API Gateway shutdown complete",
        extra={"phase": "shutdown", "shutdown_phase": "complete"},
    )

# --- 3. RATE LIMITER CONFIGURATION ---
# Use the client IP to limit requests (e.g. max 10 per minute per IP)
limiter = Limiter(key_func=get_remote_address)

# Inizializziamo FastAPI
app = FastAPI(title="ATLAS - API Gateway", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# --- 4. TELEMETRY (The hook for the agent) ---
# Automatically exposes metrics on /metrics (latency, request count, errors)
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
async def ingest_transaction(request: Request, transaction: Transaction):
    if not kafka_connected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, 
            detail="Queueing service is temporarily not ready."
        )

    try:
        # 1. Serialize the validated payload
        payload = transaction.model_dump_json().encode("utf-8")
        
        # 2. Event-driven pattern: publish to Kafka asynchronously
        await producer.send_and_wait(KAFKA_TOPIC, payload)
        
        logger.info(
            "Transaction successfully sent to Kafka",
            extra={"transaction_id": transaction.transaction_id},
        )
        
        # 3. Respond to the client quickly
        return {"status": "accepted", "transaction_id": transaction.transaction_id}
        
    except Exception as e:
        # Circuit breaker / graceful degradation pattern:
        # If Kafka is down, avoid crashing the app and return 503.
        # The agent will read this error through the /metrics endpoint.
        logger.error(
            "Failed to send transaction to Kafka",
            extra={"error": str(e), "transaction_id": transaction.transaction_id},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, 
            detail="Queueing service is temporarily unavailable."
        )

# --- 7. KUBERNETES PROBES ---

@app.get("/health/live", status_code=status.HTTP_200_OK)
async def liveness_probe():
    """
    Kubernetes Liveness Probe endpoint.
    
    Returns 200 OK if the HTTP event loop is running and responsive.
    Does NOT check Kafka connectivity.
    
    Purpose: Prevents K8s from restarting the pod while waiting for Kafka.
    Failure behavior: Restart pod (only if HTTP server is actually hung/dead).
    """
    return {"status": "alive", "phase": "liveness_check"}


@app.get("/health/ready", status_code=status.HTTP_200_OK)
async def readiness_probe():
    """
    Kubernetes Readiness Probe endpoint.
    
    Returns 200 OK ONLY if:
    1. HTTP server is running (implied by reaching this endpoint)
    2. Kafka producer is initialized
    3. Kafka broker connection is established and healthy
    
    Returns 503 Service Unavailable otherwise.
    
    Purpose: Routes traffic only to pods ready to process transactions.
    Failure behavior: Remove pod from load balancer; do NOT restart.
    """
    # Check if Kafka has successfully connected
    if not kafka_connected:
        logger.warning(
            "Readiness check failed: Kafka not yet connected",
            extra={"phase": "readiness_check", "kafka_status": "not_connected"},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Kafka connection not yet established. Waiting for broker to become available.",
        )
    
    # Check if producer object exists (should always be true if kafka_connected is true)
    if producer is None:
        logger.error(
            "Readiness check failed: Producer not initialized",
            extra={"phase": "readiness_check"},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Kafka producer not initialized.",
        )

    try:
        # Verify broker connectivity and metadata availability
        await asyncio.wait_for(
            producer.client.force_metadata_update(), 
            timeout=2.0
        )
        logger.debug(
            "Readiness check passed: Kafka is healthy",
            extra={"phase": "readiness_check", "kafka_status": "healthy"},
        )
        return {"status": "ready", "phase": "readiness_check", "kafka_status": "connected"}
        
    except asyncio.TimeoutError:
        logger.warning(
            "Readiness check failed: Kafka metadata timeout",
            extra={"phase": "readiness_check", "timeout_seconds": 2.0},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Timeout contacting Kafka cluster. Broker may be unhealthy or overloaded.",
        )
        
    except Exception as e:
        logger.error(
            "Readiness check failed: Unexpected error during Kafka verification",
            extra={
                "phase": "readiness_check",
                "error_type": type(e).__name__,
                "error_message": str(e),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Kafka readiness verification failed: {type(e).__name__}",
        )