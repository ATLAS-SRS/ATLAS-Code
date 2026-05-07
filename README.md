
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

Run Locust against the ingress host:

```bash
locust -f locust/locustfile.py --host=http://fraud-api.127.0.0.1.nip.io
```

Quick verification before starting Locust:

```bash
python3 - <<'PY'
import urllib.request
with urllib.request.urlopen('http://fraud-api.127.0.0.1.nip.io/health/live', timeout=3) as r:
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

## SRE Guardian & Trend Reporting

The platform uses the `agents-devops` SRE Guardian runtime for alert intake, investigation, remediation reasoning, and post-mortem reporting, combined with the Trend Reporting Agent for incident pattern analysis.

### Guardian Container
- Runs from `agents-devops/main_agent_guardian.py`
- Exposes a FastAPI webhook on port `8000`
- Spawns `agents-devops/k8s_mcp.py` locally over stdio for Kubernetes control
- Investigates alerts with Grafana MCP and executes remediation reasoning with LangGraph

### Trend Reporting Agent (Dashboard + Health Server)
- **Multi-process orchestration** via `agents-devops/entrypoint.sh`:
  - `uvicorn main_agent_guardian:app` on port `8000` (FastAPI webhook)
  - `python src/agent_trend/health_server.py` on port `8002` (liveness + readiness probes)
  - `streamlit run src/agent_trend/dashboard.py` on port `8501` (interactive post-mortem dashboard)
- **Dashboard features**:
  - Real-time incident visualization via Streamlit
  - Trend analysis with LLM-generated insights
  - Exposed through Kubernetes ingress at `http://guardian-report.127.0.0.1.nip.io`
- **Health checks**:
  - `/health` endpoint: Always returns 200 (liveness probe)
  - `/ready` endpoint: Returns 503 if database unreachable (readiness probe)
  - Used by Kubernetes for pod lifecycle management


### Configuration & Environment Variables

**Guardian Runtime Variables**:
- `TARGET_DEPLOYMENTS`: Kubernetes deployments to monitor
- `NAMESPACE`: Kubernetes namespace (default: `default`)
- `LM_STUDIO_URL` / `LLM_API_URL`: OpenAI-compatible LLM endpoint
- `LLM_MODEL` / `LM_MODEL`: Model name to use (e.g., `gpt-4`, `google/gemma-3-12b`)
- `LLM_API_KEY`: API key (or "local-no-key" for local models)
- `DATABASE_URL`: PostgreSQL connection string (required)
- `MAX_TOOL_STEPS`: Maximum reasoning steps per incident
- `RPS_REPLICA_THRESHOLDS`: CPU/memory scaling thresholds
- `REPORTS_DIR`: Output directory for incident reports

**Validation**:
- `DATABASE_URL` and either `LLM_API_URL` or `LM_STUDIO_URL` are required at startup
- Missing required variables cause immediate failure with clear error messages
- Config validation runs at module import time in `src/agent_guardian/config.py`

### Runtime Behavior
- FastAPI accepts Alertmanager webhooks immediately and processes firing alerts in background tasks
- Reports are written to `reports/incidents/` and upserted by incident key, so repeated alerts update the same incident record
- The dashboard is exposed through ingress at `http://guardian-report.127.0.0.1.nip.io`
- The webhook endpoint stays internal to the cluster at `http://atlas-guardian-service.default.svc.cluster.local:8000/webhook`
- Health probes on port `8002` allow Kubernetes to monitor pod readiness and restart unhealthy instances

### LM Studio Support
- `LM_STUDIO_URL`, `LMSTUDIO_BASE_URL`, or `LLM_API_URL` point to the OpenAI-compatible local model server
- `LM_MODEL` or `LMSTUDIO_MODEL` should match the exact model name exposed by LM Studio, such as `google/gemma-3-12b`
- The Kubernetes manifest uses `http://host.docker.internal:1234/v1` in Docker Desktop
- Invalid model output is handled safely inside the client loop

### Critical Production Fixes

The following issues have been identified and resolved to ensure system reliability:

**1. Silent Exception Handling** (`agents-devops/src/agent_trend/trend_analyzer.py`)
- Changed bare `except Exception:` to `except ImportError:` with logged error message
- Impact: Import errors now surfaced immediately instead of silently swallowed
- Status: ✅ Fixed and tested

**2. Environment Variable Validation** (`agents-devops/src/agent_guardian/config.py`)
- Added `_validate_config()` function called at module import time
- Checks: `DATABASE_URL` required, either `LLM_API_URL` or `LM_STUDIO_URL` required
- Impact: Missing critical env vars detected at startup with clear error messages
- Status: ✅ Fixed and validated

**3. Duplicate Dashboard Removal**
- Consolidated multiple dashboard implementations into single source of truth: `agents-devops/src/agent_trend/dashboard.py`
- Impact: Reduced maintenance burden and confusion about active dashboard version
- Status: ✅ Resolved

**4. Kubernetes Health Checks** (`agents-devops/src/agent_trend/health_server.py`)
- Created dedicated FastAPI health server on port `8002`
- Endpoints: `/health` (liveness), `/ready` (readiness with database check)
- K8s probes configured with appropriate timeouts: 30s initial delay, 10s period for liveness; 15s delay, 5s period for readiness
- Status: ✅ Deployed and working

### Deployment Checklist

When deploying to Kubernetes with `bash deploy.sh --build`:

- [ ] Verify Docker images build successfully (check output for "Images built successfully")
- [ ] Confirm all Kubernetes manifests applied (check for "unchanged" or "configured" status)
- [ ] Wait for all pod rollouts: `kubectl get pods -w`
- [ ] Test API Gateway health: `curl http://fraud-api.127.0.0.1.nip.io/health/live`
- [ ] Test Guardian webhook health: `kubectl exec -it deployment/atlas-guardian-agent -- curl localhost:8000/health`
- [ ] Test health probes on dashboard pod: `kubectl port-forward svc/atlas-guardian-dashboard-service 8002:8002 && curl localhost:8002/ready`
- [ ] Verify Grafana Mcp server is running: `kubectl logs deployment/grafana-mcp-server | tail -20`
- [ ] Access Guardian dashboard at `http://guardian-report.127.0.0.1.nip.io`
- [ ] Confirm Locust ConfigMap mounted (check `kubectl describe configmap locust-script`)
- [ ] Start Locust: `locust -f locust/locustfile.py --host=http://fraud-api.127.0.0.1.nip.io`

### Development
- Update `agents-devops/k8s_mcp.py` when you need new Kubernetes tools
- Update `agents-devops/src/agent_trend/trend_analyzer.py` for incident trend analysis logic
- Update `agents-devops/src/agent_trend/dashboard.py` for dashboard UI changes
- Update `agents-devops/entrypoint.sh` when changing process orchestration (uvicorn, health server, streamlit)
- Update `k8s/app-layer/agent-trend-report.yaml` for Kubernetes deployment configuration

### Rebuild And Redeploy Guardian Agent
```bash
# Rebuild Docker image with entrypoint.sh
docker build -t atlas/agent-guardian:latest agents-devops

# Apply Kubernetes manifests (ServiceAccount, RBAC, Deployments, Services)
kubectl apply -f k8s/app-layer/agent-guardian.yaml
kubectl apply -f k8s/app-layer/agent-trend-report.yaml
kubectl apply -f k8s/app-layer/ingress.yaml

# Trigger rollout with new image
kubectl rollout restart deployment/atlas-guardian-agent
kubectl rollout status deployment/atlas-guardian-agent --timeout=120s

# Verify all pods ready
kubectl get pods -l app=atlas-guardian-agent -l app=atlas-guardian-dashboard

# Access dashboard
echo "Dashboard: http://guardian-report.127.0.0.1.nip.io"
```

### Monitoring Guardian Agent

**Check logs for startup errors**:
```bash
kubectl logs deployment/atlas-guardian-dashboard -f | grep -E "(ERROR|WARN|health)"
```

**Verify health probes working**:
```bash
kubectl port-forward svc/atlas-guardian-dashboard-service 8002:8002
curl -v http://localhost:8002/health    # Liveness
curl -v http://localhost:8002/ready     # Readiness (checks DB)
```

**Restart unhealthy pod**:
```bash
kubectl delete pod -l app=atlas-guardian-dashboard
```

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
├── agents-devops/         # SRE Guardian webhook + dashboard
├── monitoring/           # Prometheus config
└── docker-compose.yml    # Orchestration
```

### Adding New Features
1. Define Kubernetes MCP tools in `agents-devops/k8s_mcp.py`
2. Update `agents-devops/src/agent_guardian/runtime.py` if the reasoning loop or report schema changes
3. Update `k8s/app-layer/agent-guardian.yaml` for runtime configuration changes
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
