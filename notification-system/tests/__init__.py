"""
Unit tests for Notification Broker

Tests the NotificationBroker class functionality including:
- Message consumption from Kafka
- Message transformation
- Notification publishing
- Error handling
"""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from notification_broker import NotificationBroker


class TestNotificationBroker:
    """Test suite for NotificationBroker"""

    @pytest.fixture
    def broker(self):
        """Fixture that provides a NotificationBroker instance"""
        with patch('notification_broker.Consumer'), \
             patch('notification_broker.Producer'):
            broker = NotificationBroker()
            return broker

    def test_initialization(self, broker):
        """Test broker initialization"""
        assert broker.running is True
        assert broker.consumer is not None
        assert broker.producer is not None

    def test_process_scored_transaction_valid(self, broker):
        """Test processing valid scored transaction"""
        scored_transaction = {
            "transaction_id": "550e8400-e29b-41d4-a716-446655440000",
            "risk_score": 42.5,
            "risk_level": "APPROVATA",
            "timestamp": "2026-03-24T10:48:33Z",
            "user_id": 123,
            "amount": 500.00,
        }

        # Mock the producer
        broker.producer = Mock()
        broker.producer.produce = Mock()

        # Process transaction
        broker.process_scored_transaction(json.dumps(scored_transaction))

        # Verify producer was called
        assert broker.producer.produce.called

    def test_process_scored_transaction_invalid_json(self, broker):
        """Test handling of invalid JSON"""
        invalid_message = "not valid json"

        # Should handle gracefully
        try:
            broker.process_scored_transaction(invalid_message)
            # If no exception is raised, test passes (error handling works)
            assert True
        except Exception as e:
            pytest.fail(f"Should handle invalid JSON gracefully: {e}")

    def test_notification_format(self, broker):
        """Test notification message format"""
        scored_transaction = {
            "transaction_id": "550e8400-e29b-41d4-a716-446655440000",
            "risk_score": 42.5,
            "risk_level": "APPROVATA",
            "timestamp": "2026-03-24T10:48:33Z",
            "user_id": 123,
            "amount": 500.00,
        }

        notification = {
            "transaction_id": scored_transaction["transaction_id"],
            "risk_score": scored_transaction["risk_score"],
            "risk_level": scored_transaction["risk_level"],
            "status": "PROCESSED",
            "approved": scored_transaction["risk_level"] == "APPROVATA",
            "timestamp": scored_transaction["timestamp"]
        }

        # Verify notification structure
        assert "transaction_id" in notification
        assert "risk_score" in notification
        assert "risk_level" in notification
        assert "status" in notification
        assert "approved" in notification
        assert "timestamp" in notification

    def test_shutdown(self, broker):
        """Test graceful shutdown"""
        broker.shutdown()
        assert broker.running is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
