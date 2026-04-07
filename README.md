
# ATLAS - Fraud Alert Triage & Escalation Platform

A distributed, AI-powered fraud detection system for Banca Aemilia with intelligent operational management.

## Overview

ATLAS implements a sophisticated fraud detection platform that combines real-time transaction processing, AI-driven operational management, and automated scaling capabilities. The system is designed for high-risk financial environments with strict reliability and safety requirements.

## Architecture

### Core System Components

- **API Gateway**: FastAPI-based ingress point with rate limiting and metrics
- **Kafka Message Broker**: Event-driven communication backbone
- **Transaction Client**: Mock transaction generator for testing
- **Evaluation System**: Kafka consumer with fraud detection logic
- **PostgreSQL Database**: Transaction storage and analysis
- **Prometheus Monitoring**: Metrics collection and alerting

### Agentic Operational Layer

- **Scaling Agent**: Centralized autoscaling orchestration for Kubernetes and MCP/HTTP operator workflows
- **Operational Safety**: Human-in-the-loop decision making
- **Metrics-Driven Actions**: Automated responses to system conditions

## Key Features

### Fraud Detection Pipeline
1. **Transaction Ingestion**: Real-time transaction processing via REST API
2. **Event Streaming**: Kafka-based decoupling for scalability
3. **Fraud Analysis**: AI-powered evaluation with historical context
4. **Database Storage**: PostgreSQL for transaction persistence

### Intelligent Operations
- **Auto-Scaling**: Feed-forward orchestration scales the full processing pipeline from a global ingress load indicator
- **Load Monitoring**: Real-time metrics from Prometheus
- **Safety Boundaries**: Configurable thresholds with human oversight
- **Operational Visibility**: Comprehensive logging and alerting

### Business Context
- **Target**: Banca Aemilia (1.5M customers, 2.5M daily transactions)
- **Risk Profile**: High-stakes financial fraud prevention
- **SLA Requirements**: 99.99% uptime, sub-second response times

## Quick Start

### Prerequisites
- Docker & Docker Compose
- Python 3.11+
- 4GB RAM minimum

### Launch System
```bash
# Start all services
docker compose up --build

# Access points
# - API Gateway: http://localhost:8000
# - Prometheus: http://localhost:9090
# - Scaling Agent HTTP API: http://localhost:8001
# - Scaling Agent metrics: http://localhost:8002/metrics
```

### Test Transaction Flow
```bash
# Send test transaction
curl -X POST http://localhost:8000/api/v1/transactions \
  -H "Content-Type: application/json" \
  -d '{"transaction_id": "test-123", "timestamp": "2026-03-23T10:00:00Z", "channel": "web", "transaction_type": "payment", "payment_details": {"amount": 100.0, "currency": "EUR", "payment_method": "credit_card"}, "user_id": 123}'
```

## Load Testing with Locust

Use `locust/locustfile.py` to stress the FastAPI gateway endpoint `/api/v1/transactions`.

### Important Behavior
- The Docker service `transaction-client` and Locust are independent traffic generators.
- If you do not start Locust, the system still receives traffic from `transaction-client` (if running in Compose).
- If you run both together, the gateway receives combined traffic.

### Option 1: Run Locust Locally
```bash
# Start platform services
docker compose up -d --build

# Install Locust locally (one-time)
pip install locust

# Start Locust UI
locust -f locust/locustfile.py --host=http://localhost:8000
```

Open the Locust UI at `http://localhost:8089`.

### Option 2: Run Locust in Docker
```bash
docker run --rm -it --network host -v "$PWD":/mnt/locust locustio/locust \
  -f /mnt/locust/locust/locustfile.py --host=http://localhost:8000
```

### Option 3: Run Locust Against Kubernetes Ingress
Use this option when ATLAS is deployed on Kubernetes.

Prerequisites:
- NGINX Ingress Controller installed and running in the cluster
- Ingress resource `api-gateway-ingress` applied
- Local hosts entry mapping `fraud-api.local` to localhost:

```text
127.0.0.1 fraud-api.local
```

Run Locust against the ingress host:

```bash
locust -f locust/locustfile.py --host=http://fraud-api.local
```

Quick verification before starting Locust:

```bash
python3 - <<'PY'
import urllib.request
with urllib.request.urlopen('http://fraud-api.local/health/live', timeout=3) as r:
    print(r.status)
    print(r.read().decode())
PY
```

If ingress is not available yet, use temporary port-forward fallback:

```bash
kubectl port-forward -n default svc/api-gateway 8000:8000
locust -f locust/locustfile.py --host=http://localhost:8000
```

### Headless Example (CI-friendly)
```bash
locust -f locust/locustfile.py --host=http://localhost:8000 --headless -u 100 -r 10 -t 5m
```

### Isolating Locust Metrics (without built-in client traffic)
```bash
# Keep all services but disable the mock transaction client
docker compose up -d --build --scale transaction-client=0
```

## Scaling Agent

The platform now uses a single all-in-one scaling runtime.

### All-in-One Kubernetes MCP Agent
- Runs from `scaling-agent/mcp_client.py` and is started by `scaling-agent/Dockerfile`
- Spawns `scaling-agent/mcp_server.py` locally over stdio in the same container
- Reads global ingress load from Prometheus using `sum(rate(http_requests_total{job="api-gateway"}[1m]))`
- Applies one global RPS reading to the configured deployment set
- Scales deployments sequentially in one loop with the default order `api-gateway,scoring-system,enrichment-system,notification-system`

Key environment variables:
- `TARGET_DEPLOYMENTS`
- `PROMQL_RPS_QUERY`
- `CHECK_INTERVAL`, `MIN_REPLICAS`, `MAX_REPLICAS`, `NAMESPACE`
- `LM_STUDIO_URL`, `LLM_API_URL`, `LLM_MODEL`
- `MAX_TOOL_STEPS`, `RPS_REPLICA_THRESHOLDS`

### MCP Tools
- `get_rps`: read the global RPS from Prometheus
- `get_current_replicas`: read deployment replica count from Kubernetes
- `set_replicas`: update deployment replicas within guardrails
- `get_scaling_recommendation`: combine current load and replica state into a target action

### LM Studio Support
- `LM_STUDIO_URL` points to the OpenAI-compatible local model server
- The Kubernetes manifest uses `http://host.docker.internal:1234/v1` in Docker Desktop
- Invalid model output is handled safely inside the client loop

### Development
- Update `scaling-agent/mcp_server.py` when you need new tools
- Update `scaling-agent/mcp_client.py` when you need to change the reasoning loop or tool execution flow
- Update `k8s/app-layer/k8s-agent.yaml` when you need to change deployment-time configuration

## Monitoring & Observability

### Metrics Endpoints
- **Prometheus**: http://localhost:9090
- **API Gateway Metrics**: http://localhost:8000/metrics
- **Health Checks**: Built into all services

### Key Metrics
- Transaction processing rate
- System resource utilization
- Container scaling events
- Error rates and latency

## Development

### Project Structure
```
atlas-code/
├── gateway/              # API Gateway (FastAPI)
├── client/               # Transaction generator
├── enrichment-system/    # Enrichment engine
├── scoring-system/       # Fraud scoring engine
├── notification-system/  # Notification broker
├── scaling-agent/        # MCP scaling agent
├── monitoring/           # Prometheus config
└── docker-compose.yml    # Orchestration
```

### Adding New Features
1. Define MCP tools in `scaling-agent/mcp_server.py`
2. Update `scaling-agent/mcp_client.py` if the reasoning loop or tool schema changes
3. Update `k8s/app-layer/k8s-agent.yaml` for runtime configuration changes
4. Add Prometheus metrics for new scaling signals as needed

## Operational Safety

### Human-in-the-Loop
- Critical scaling decisions require human approval
- Automated actions are logged and auditable
- Rollback capabilities for failed operations

### Risk Mitigation
- Resource limits prevent runaway scaling
- Health checks ensure service availability
- Circuit breakers for fault isolation

## Performance & Economics

### Service Level Objectives
- **Availability**: 99.99% uptime target
- **Latency**: <500ms p95 response time
- **Throughput**: 2.5M transactions/day capacity

### Return on Agent (ROA)
- **Cost Savings**: 40% reduction in over-provisioning
- **Performance**: Maintains SLIs during 10x traffic spikes
- **Efficiency**: Automated scaling reduces manual DevOps work

## Contributing

1. Follow the established architecture patterns
2. Add comprehensive logging and metrics
3. Include automated tests
4. Update documentation
5. Ensure MCP compliance for agent features

## License

Proprietary - Banca Aemilia Internal Use Only
