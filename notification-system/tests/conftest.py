"""
Conftest for notification system tests

Provides fixtures and configuration for pytest suite.
"""

import pytest


@pytest.fixture
def kafka_config():
    """Fixture providing Kafka configuration"""
    return {
        "bootstrap.servers": "localhost:9092",
        "group.id": "notification_test_group",
    }


@pytest.fixture
def sample_scored_transaction():
    """Fixture providing sample scored transaction"""
    return {
        "transaction_id": "550e8400-e29b-41d4-a716-446655440000",
        "user_id": 123,
        "amount": 500.00,
        "merchant_id": "merchant_456",
        "transaction_date": "2026-03-24",
        "transaction_time": "10:48:33",
        "country": "IT",
        "risk_score": 42.5,
        "risk_level": "APPROVATA",
        "latitude": 41.9028,
        "longitude": 12.4964,
        "timestamp": "2026-03-24T10:48:33Z"
    }


@pytest.fixture
def high_risk_transaction():
    """Fixture providing high-risk transaction"""
    return {
        "transaction_id": "660e8400-e29b-41d4-a716-446655440001",
        "user_id": 456,
        "amount": 5000.00,
        "risk_score": 85.0,
        "risk_level": "FRODE",
        "timestamp": "2026-03-24T10:50:33Z"
    }
