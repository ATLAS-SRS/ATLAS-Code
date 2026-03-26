import time
import random
import requests
import os
import uuid
from datetime import datetime, timezone
import threading
import json
from collections import defaultdict
from confluent_kafka import Consumer, KafkaException
from health_probe import HealthServer

# Configuration from environment variables
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8000/api/v1/transactions")
SLEEP_TIME = float(os.getenv("SLEEP_TIME", 2.0))
KAFKA_BROKER = os.getenv("KAFKA_BROKER", "localhost:9092")
KAFKA_NOTIFICATION_TOPIC = os.getenv("KAFKA_NOTIFICATION_TOPIC", "transaction-notifications")

# Transaction tracking
pending_transactions = {}  # Maps transaction_id to transaction details
transaction_lock = threading.Lock()

# Health check timestamps
last_send_time = time.time()
last_consume_time = time.time()

# Global consumer thread for readiness check
consumer_thread = None

def generate_ip_address():
    """Generates a simple public IPv4 address."""
    first_octet = random.choice([8, 23, 45, 52, 63, 80, 91, 101, 121, 151, 185, 203])
    return f"{first_octet}.{random.randint(1, 254)}.{random.randint(1, 254)}.{random.randint(1, 254)}"

def generate_transaction():
    """Generates a transaction payload aligned with the Gateway's model."""
    return {
        "transaction_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "channel": random.choice(["web", "mobile", "app", "pos"]),
        "transaction_type": random.choice(["payment", "refund", "subscription"]),
        "payment_details": {
            "amount": round(random.uniform(5.0, 500.0), 2),
            "currency": random.choice(["EUR", "USD", "GBP"]),
            "payment_method": random.choice(["credit_card", "paypal", "apple_pay"]),
        },
        "ip_address": generate_ip_address(),
        "user_id": random.randint(100, 999),
    }

def process_notification(notification: dict) -> None:
    """Process a received notification and make a decision.
    
    Args:
        notification: The notification payload from Kafka
    """
    transaction_id = notification.get("transaction_id", "N/A")
    short_id = transaction_id[:8] if isinstance(transaction_id, str) else "N/A"
    
    # Check if this is an approved transaction
    if notification.get("status") == "ERROR":
        error_reason = notification.get("error_reason", "Unknown error")
        print(f"❌ Transaction {short_id} ERROR: {error_reason}")
        with transaction_lock:
            pending_transactions.pop(transaction_id, None)
        return
    
    risk_score = notification.get("risk_score")
    risk_level = notification.get("risk_level")
    approved = notification.get("approved", False)
    
    with transaction_lock:
        tx_details = pending_transactions.pop(transaction_id, None)
    
    if approved or risk_level == "APPROVATA":
        print(f"✅ Transaction {short_id} APPROVED | Score: {risk_score} | Level: {risk_level}")
        if tx_details:
            print(f"   💰 Amount: {tx_details.get('payment_details', {}).get('amount')} {tx_details.get('payment_details', {}).get('currency')}")
    else:
        print(f"❌ Transaction {short_id} REFUSED | Score: {risk_score} | Level: {risk_level}")
        if tx_details:
            print(f"   💰 Amount: {tx_details.get('payment_details', {}).get('amount')} {tx_details.get('payment_details', {}).get('currency')}")
            print(f"   ⚠️  Reason: {risk_level}")

def consume_and_process_notifications():
    """Kafka consumer to process transaction notifications."""
    global last_consume_time
    conf = {
        "bootstrap.servers": KAFKA_BROKER,
        "group.id": "client_notification_group",
        "auto.offset.reset": "earliest",
    }
    consumer = Consumer(conf)
    consumer.subscribe([KAFKA_NOTIFICATION_TOPIC])

    print(f"👂 Listening for transaction notifications on topic '{KAFKA_NOTIFICATION_TOPIC}'...")

    while True:
        last_consume_time = time.time()
        msg = consumer.poll(timeout=1.0)
        if msg is None:
            continue
        if msg.error():
            if msg.error().code() != KafkaException._PARTITION_EOF:
                print(f"🔥 Kafka Error: {msg.error()}")
            continue

        try:
            notification = json.loads(msg.value().decode("utf-8"))
            process_notification(notification)

        except json.JSONDecodeError:
            print("Could not decode notification message:", msg.value())
        except Exception as e:
            print(f"An unexpected error occurred processing notification: {e}")

def check_liveness():
    """Check if the service is live based on recent activity."""
    current_time = time.time()
    send_recent = (current_time - last_send_time) < (SLEEP_TIME * 5)
    consume_recent = (current_time - last_consume_time) < 30
    return send_recent and consume_recent

def check_readiness():
    """Check if the service is ready (consumer thread is alive)."""
    return consumer_thread.is_alive()

def send_transaction():
    """Send a transaction to the gateway."""
    global last_send_time
    last_send_time = time.time()
    payload = generate_transaction()
    transaction_id = payload["transaction_id"]
    
    try:
        response = requests.post(GATEWAY_URL, json=payload, timeout=5)
        if response.status_code == 202:
            with transaction_lock:
                pending_transactions[transaction_id] = payload
            print(f"📤 Sent ID {transaction_id[:8]}... | Status: 202 ACCEPTED")
        else:
            print(f"⚠️ Sent ID {transaction_id[:8]}... | Unexpected Status: {response.status_code}")
            if response.text:
                print(f"   Response: {response.text[:100]}")
    except requests.exceptions.RequestException as e:
        print(f"❌ Gateway connection error: {e}")

def main():
    global consumer_thread
    print(f"🚀 Starting Client with Notification System")
    print(f"   Gateway: {GATEWAY_URL}")
    print(f"   Notification Topic: {KAFKA_NOTIFICATION_TOPIC}")
    print(f"   Transaction Send Interval: {SLEEP_TIME}s")
    print()

    # Start Kafka consumer in a background thread
    consumer_thread = threading.Thread(target=consume_and_process_notifications, daemon=True)
    consumer_thread.start()

    # Start health probe server
    health_server = HealthServer(liveness_check_fn=check_liveness, readiness_check_fn=check_readiness)
    health_server.start()

    # Give consumer time to connect
    time.sleep(2)

    # Main loop: send transactions
    while True:
        send_transaction()
        time.sleep(SLEEP_TIME)

if __name__ == "__main__":
    main()
