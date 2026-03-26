"""
Locust Load Testing Script for ATLAS Fraud Detection System
============================================================

This script simulates realistic user behavior by sending transaction requests
to the API Gateway endpoint /api/v1/transactions, allowing stress testing
of the entire fraud detection pipeline.

Usage:
    locust -f locustfile.py --host=http://localhost:8000

Or with specific workers:
    locust -f locustfile.py --host=http://localhost:8000 -u 100 -r 10 -t 5m
    (100 users, spawn rate 10 users/sec, duration 5 minutes)
"""

import json
import random
import uuid
from datetime import datetime, timezone
from locust import HttpUser, task, between, events
from typing import Dict, Any


# ============================================================================
# HELPER FUNCTIONS FOR DATA GENERATION
# ============================================================================

def generate_ip_address() -> str:
    """
    Generates a realistic public IPv4 address.
    Uses common ISP first octets to simulate diverse geographic origin.
    
    Returns:
        str: A valid IPv4 address
    """
    first_octets = [8, 23, 45, 52, 63, 80, 91, 101, 121, 151, 185, 203]
    first_octet = random.choice(first_octets)
    return f"{first_octet}.{random.randint(1, 254)}.{random.randint(1, 254)}.{random.randint(1, 254)}"


def generate_transaction() -> Dict[str, Any]:
    """
    Generates a complete transaction payload matching the Gateway's expected schema.
    Payload structure mirrors the Transaction and PaymentDetails Pydantic models.
    
    Returns:
        dict: A complete transaction payload ready for POST to /api/v1/transactions
    """
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


# ============================================================================
# METRICS EVENT HANDLERS
# ============================================================================

@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """
    Event handler called when load test starts.
    Prints initialization message.
    """
    print("\n" + "="*80)
    print("🚀 LOCUST LOAD TEST STARTED - ATLAS Fraud Detection System")
    print("="*80 + "\n")


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    """
    Event handler called when load test stops.
    Prints final statistics summary.
    """
    print("\n" + "="*80)
    print("🛑 LOCUST LOAD TEST STOPPED")
    print("="*80 + "\n")


# ============================================================================
# MAIN USER CLASS
# ============================================================================

class TransactionUser(HttpUser):
    """
    Simulates a user sending transaction requests to the fraud detection API Gateway.
    
    Attributes:
        wait_time: Time between tasks (realistic 0.5-2.0 seconds between transactions)
    """

    wait_time = between(0.5, 2.0)

    def on_start(self):
        """
        Called when a Locust user is created and starts running.
        Can be used for initialization (login, setup, etc.).
        """
        pass

    @task
    def send_transaction(self):
        """
        Main task: Send a transaction request to the API Gateway.
        
        - Generates a random transaction payload
        - Sends POST request to /api/v1/transactions
        - Validates response status codes
        - Records success/failure metrics with Locust
        
        Expected behavior:
            - 202 Accepted: Transaction successfully queued for processing
            - 503 Service Unavailable: Kafka connection issue
            - 429 Too Many Requests: Rate limit exceeded
            - Other errors: Logged as failures
        """
        # Generate realistic transaction data
        transaction_payload = generate_transaction()

        # Send POST request with error handling via context manager
        with self.client.post(
            "/api/v1/transactions",
            json=transaction_payload,
            catch_response=True,
            timeout=10,
        ) as response:
            # Success case: 202 Accepted
            if response.status_code == 202:
                response.success()
                # Optional: Log short transaction ID for debugging
                tx_id_short = transaction_payload["transaction_id"][:8]
                print(f"✅ [{tx_id_short}] Transaction accepted")

            # Rate limit case: 429 Too Many Requests
            elif response.status_code == 429:
                error_msg = f"Rate limited (429): {response.text}"
                response.failure(error_msg)
                print(f"⚠️  {error_msg}")

            # Service unavailable case: 503
            elif response.status_code == 503:
                error_msg = f"Service unavailable (503): {response.text}"
                response.failure(error_msg)
                print(f"⚠️  {error_msg}")

            # Unprocessable entity: 422 (validation error)
            elif response.status_code == 422:
                error_msg = f"Validation error (422): {response.text}"
                response.failure(error_msg)
                print(f"❌ {error_msg}")

            # Generic error handling for all other status codes
            else:
                error_msg = f"Unexpected status {response.status_code}: {response.text}"
                response.failure(error_msg)
                print(f"❌ {error_msg}")


# ============================================================================
# OPTIONAL: Advanced configuration for specialized load testing
# ============================================================================

class HighLoadTransactionUser(TransactionUser):
    """
    Extended user class for high-load stress testing.
    Generates more frequent requests (0.1-0.5 seconds wait time).
    """

    wait_time = between(0.1, 0.5)


class NormalLoadTransactionUser(TransactionUser):
    """
    Extended user class for normal load simulation.
    Generates realistic transaction frequency (1-3 seconds wait time).
    """

    wait_time = between(1.0, 3.0)
