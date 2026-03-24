# Notification System

Standalone microservice broker for ATLAS fraud detection system notifications.

## Quick Start

### Docker Compose
```bash
docker-compose up -d notification-system
```

### Local Development
```bash
pip install -r requirements.txt
export KAFKA_BROKER=localhost:9092
python notification_broker.py
```

## Configuration

Environment variables:
- `KAFKA_BROKER` - Kafka broker address (default: localhost:9092)
- `KAFKA_SCORED_TOPIC` - Input topic (default: scored-transactions)
- `KAFKA_NOTIFICATION_TOPIC` - Output topic (default: transaction-notifications)
- `KAFKA_GROUP_ID` - Consumer group (default: notification_broker_group)

See `.env.example` for full configuration.

## Features

- Consumes scored transactions from Kafka
- Transforms and publishes notifications to clients
- Graceful error handling and recovery
- Production-ready with Docker support
