import asyncio
import json
import logging
import os
import threading
from typing import List, Optional

from confluent_kafka import Consumer, KafkaError
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer
from confluent_kafka.serialization import SerializationContext, MessageField

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("notification-system")

KAFKA_BROKER = os.getenv("KAFKA_BROKER", "localhost:9092")
KAFKA_SCORED_TOPIC = os.getenv("KAFKA_SCORED_TOPIC", "scored-transactions")
KAFKA_GROUP_ID = os.getenv("KAFKA_GROUP_ID", "notification-gateway-group")
SCHEMA_REGISTRY_URL = os.getenv("SCHEMA_REGISTRY_URL", "http://schema-registry.default.svc.cluster.local:8081")

sr_client = SchemaRegistryClient({"url": SCHEMA_REGISTRY_URL})
avro_deserializer = AvroDeserializer(sr_client)

class ConnectionManager:
    def __init__(self) -> None:
        self._active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._active_connections.append(websocket)
        logger.info("WebSocket connected", extra={"active": len(self._active_connections)})

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self._active_connections:
            self._active_connections.remove(websocket)
            logger.info(
                "WebSocket disconnected", extra={"active": len(self._active_connections)}
            )

    async def broadcast(self, message: dict) -> None:
        if not self._active_connections:
            return
        to_remove: List[WebSocket] = []
        for websocket in list(self._active_connections):
            try:
                await websocket.send_json(message)
            except Exception:
                to_remove.append(websocket)
        for websocket in to_remove:
            self.disconnect(websocket)


app = FastAPI()
manager = ConnectionManager()

_kafka_thread: Optional[threading.Thread] = None
_kafka_stop_event = threading.Event()
_event_loop: Optional[asyncio.AbstractEventLoop] = None


def _build_consumer() -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": KAFKA_BROKER,
            "group.id": KAFKA_GROUP_ID,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": True,
        }
    )


def _parse_message(payload: bytes) -> Optional[dict]:
    data = None
    
    # 1. Prova a decodificare come Avro (i messaggi Confluent iniziano con byte 0)
    if payload and payload[0] == 0:
        try:
            ctx = SerializationContext(KAFKA_SCORED_TOPIC, MessageField.VALUE)
            data = avro_deserializer(payload, ctx)
        except Exception as exc:
            logger.warning("Avro decode failed", extra={"error": str(exc)})
    
    # 2. Fallback: Prova come JSON testuale normale
    if data is None:
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("Invalid payload format (neither Avro nor JSON)", extra={"error": str(exc)})
            return None

    # 3. Gestisci l'inner payload (se il messaggio originale conteneva una stringa JSON annidata)
    if isinstance(data.get("payload"), str):
        try:
            inner_payload = json.loads(data["payload"])
            data = {**inner_payload, **data}
        except json.JSONDecodeError:
            pass

    # 4. Estrai i campi
    transaction_id = data.get("transaction_id")
    risk_score = data.get("risk_score")
    risk_level = data.get("risk_level")
    
    if transaction_id is None or risk_level is None:
        logger.warning("Missing required fields", extra={"data": data})
        return None

    try:
        risk_score_value = float(risk_score) if risk_score is not None else 0.0
    except (TypeError, ValueError):
        risk_score_value = 0.0

    approved = str(risk_level).upper() in {"APPROVED", "LOW"}

    return {
        "transaction_id": str(transaction_id),
        "risk_score": risk_score_value,
        "risk_level": str(risk_level),
        "status": "PROCESSED",
        "approved": approved,
    }


def _kafka_consumer_loop() -> None:
    consumer = _build_consumer()
    try:
        consumer.subscribe([KAFKA_SCORED_TOPIC])
        logger.info("Kafka consumer started", extra={"topic": KAFKA_SCORED_TOPIC})

        while not _kafka_stop_event.is_set():
            message = consumer.poll(1.0)
            if message is None:
                continue
            if message.error():
                if message.error().code() != KafkaError._PARTITION_EOF:
                    logger.error(
                        "Kafka error",
                        extra={"error": str(message.error())},
                    )
                continue

            payload = _parse_message(message.value())
            if payload is None or _event_loop is None:
                continue

            try:
                asyncio.run_coroutine_threadsafe(
                    manager.broadcast(payload), _event_loop
                )
            except Exception as exc:
                logger.error("Failed to enqueue broadcast", extra={"error": str(exc)})
    except Exception as exc:
        logger.exception("Kafka consumer crashed", extra={"error": str(exc)})
    finally:
        consumer.close()
        logger.info("Kafka consumer closed")


@app.on_event("startup")
async def _on_startup() -> None:
    global _kafka_thread, _event_loop
    _event_loop = asyncio.get_running_loop()
    _kafka_stop_event.clear()
    _kafka_thread = threading.Thread(target=_kafka_consumer_loop, daemon=True)
    _kafka_thread.start()
    logger.info("Notification service started")


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    _kafka_stop_event.set()
    if _kafka_thread is not None:
        _kafka_thread.join(timeout=5)
    logger.info("Notification service stopped")


@app.get("/health/live")
async def health_live() -> dict:
    return {"status": "alive"}


@app.get("/health/ready")
async def health_ready() -> dict:
    return {"status": "ready"}


@app.websocket("/ws/notifications")
async def websocket_notifications(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("notification_system:app", host="0.0.0.0", port=8080)
