# ATLAS Scaling Agent

The `scaling-agent/` directory contains two scaling runtimes used in ATLAS:
- `agent.py`: Kubernetes central orchestrator (single-loop, multi-deployment)
- `scaling_server.py`: Compose daemon + MCP server for operator workflows (`daemon`/`stdio`)

## Overview

### Kubernetes Central Orchestrator (`agent.py`)

This runtime implements a feed-forward autoscaling model for the full fraud pipeline:
- Reads global ingress load from API Gateway once per cycle using Prometheus RPS
- Exposes internal MCP tools (`get_rps`, `get_current_replicas`, `set_replicas`) with strict typing and descriptive docstrings
- Exposes internal MCP tools (`get_rps`, `get_current_replicas`, `set_replicas`, `get_scaling_recommendation`) with strict typing and descriptive docstrings
- Uses OpenAI-compatible tool-calling (LM Studio / Ollama OpenAI API) to run an autonomous ReAct loop per deployment
- Iterates deployments sequentially (`TARGET_DEPLOYMENTS`) in a single loop
- Executes tool calls until the assistant returns a final answer without additional tool calls
- Applies replica updates with Kubernetes Deployment scale API with hard guardrails (`MIN_REPLICAS`, `MAX_REPLICAS`)

Default target list:
- `api-gateway,scoring-system,enrichment-system,notification-system`

### Compose Daemon + MCP (`scaling_server.py`)

The daemon monitors metrics and scales one configured target service in the repo-level Docker Compose runtime. In that stack, the default target service is `enrichment-system`.

When `LLM_ENABLED=true`, the daemon can consult a local model served by LM Studio through its OpenAI-compatible API. The model acts as the primary operational recommender, while the daemon still enforces cooldowns, replica guardrails, and fallback logic.

## Features

- **Feed-Forward Pipeline Scaling**: One global ingress load signal drives sequential scaling decisions across multiple microservices
- **Kubernetes Orchestration**: Native Deployment scale operations for app-layer services
- **Real-time Monitoring**: Prometheus-backed load observations
- **Safety Boundaries**: Hard guardrails enforced at tool level (`MIN_REPLICAS`, `MAX_REPLICAS`)
- **MCP Integration**: `agent.py` uses MCP-defined tools for autonomous execution; `scaling_server.py` still exposes operator-facing MCP in `stdio` mode
- **Operational Safety**: Human-in-the-loop and auditable scaling actions
- **LM Studio Support**: Optional local LLM reasoning with deterministic fallback paths

## Architecture

```
┌─────────────────┐    ┌────────────────────────┐    ┌─────────────────┐
│   Prometheus    │    │  Scaling Agent Daemon  │    │   Docker API    │
│   (Metrics)     │◄──►│   HTTP + MCP tooling   │◄──►│   (Scaling)     │
└─────────────────┘    └────────────────────────┘    └─────────────────┘
         ▲                          ▲                          ▲
         │                          │                          │
         └────────── System Metrics ──────────────┼────────────┘
```

## Kubernetes Central Loop Logic (`agent.py`)

1. Parse `TARGET_DEPLOYMENTS` into the allowed deployment set.
2. Build an OpenAI-compatible client pointing to `LLM_API_URL`.
3. Define MCP tools and expose their JSON schema to the LLM as callable tools.
4. For each deployment in order, start an autonomous conversation with the system prompt.
5. While the model emits `tool_calls`:
   - execute the corresponding local Python tool
   - append the tool result as role `tool`
   - call the model again
6. When the model stops requesting tools, record the final operational report.
7. Sleep 2 seconds between per-deployment iterations and `CHECK_INTERVAL` after a full pass.

Default RPS query:
- `sum(rate(http_requests_total{job="api-gateway"}[1m]))`

## Daemon HTTP API

The production daemon exposes:

- `GET /healthz`: service health and configuration summary
- `GET /thresholds`: current thresholds and replica guardrails
- `POST /thresholds`: update thresholds for testing/operations
- `GET /decision`: current scaling recommendation with `llm_decision`, `rule_based_decision`, `effective_decision`, `decision_source`, and `llm_status`
- `POST /scale`: manual scale action within guardrails

Prometheus metrics are exposed separately on port `8002`.

## MCP Tools

In `SCALING_MODE=stdio`, the agent exposes the following MCP tools:

- `get_current_metrics`: Get current system and application metrics
- `check_scaling_decision`: Analyze metrics and recommend scaling actions
- `scale_kafka_consumer`: Scale the configured target service (tool name kept for backward compatibility)
- `get_scaling_thresholds`: View current scaling thresholds
- `update_scaling_thresholds`: Modify scaling thresholds

## Usage

### Kubernetes Central Orchestrator

This is the runtime used by the Kubernetes manifest (`k8s/app-layer/scaling-agent.yaml`):

```bash
python -u agent.py
```

Main environment variables:
- `PROMETHEUS_URL`
- `NAMESPACE`
- `TARGET_DEPLOYMENTS`
- `PROMQL_RPS_QUERY`
- `CHECK_INTERVAL`
- `MIN_REPLICAS` / `MAX_REPLICAS`
- `LLM_API_URL` / `LLM_MODEL` / `LLM_TIMEOUT` / `LLM_TEMPERATURE` / `LLM_API_KEY`
- `MAX_TOOL_STEPS`
- `AGENT_IDLE_RPS_HINT`
- `RPS_REPLICA_THRESHOLDS`

### Manual Operation
```bash
# Run the MCP server for operator tooling
SCALING_MODE=stdio python scaling_server.py
```

### Automated Operation
The daemon runs continuously inside Docker Compose and automatically scales based on metrics:

```bash
docker compose up --build
```

Set `LLM_ENABLED=true` before `docker compose up` to delegate scaling recommendations to the LM Studio-backed agent. In the repo-level Compose stack, `TARGET_SERVICE` defaults to `enrichment-system`.

Optional local watcher:

```bash
python auto_scaler.py
```

## Tests

Repository tests now live in `scaling-agent/tests/`.

Run them from the repo root:

```bash
docker compose run --rm --entrypoint pytest scaling-agent -o cache_dir=/tmp/pytest-cache /workspace/scaling-agent/tests
```

## Configuration

### `agent.py` (Kubernetes Central Orchestrator)
- `PROMETHEUS_URL`: Prometheus server URL
- `NAMESPACE`: Kubernetes namespace containing target Deployments
- `TARGET_DEPLOYMENTS`: comma-separated deployment names to evaluate sequentially
- `PROMQL_RPS_QUERY`: global ingress RPS query (default targets `api-gateway`)
- `CHECK_INTERVAL`: full-loop interval in seconds
- `MIN_REPLICAS` / `MAX_REPLICAS`: scaling guardrails enforced by tool validation
- `LLM_API_URL`, `LLM_MODEL`, `LLM_TIMEOUT`, `LLM_TEMPERATURE`, `LLM_API_KEY`: local LLM API settings (OpenAI-compatible)
- `MAX_TOOL_STEPS`: hard cap for the autonomous tool loop in a single deployment cycle
- `AGENT_IDLE_RPS_HINT`: low-traffic hint used to trigger an additional agent reconsideration pass when a deployment remains at `MAX_REPLICAS` (default `0.05`)
- `RPS_REPLICA_THRESHOLDS`: 4 ascending comma-separated RPS thresholds used to map traffic into a 1..5 target replica band (default `5,15,30,60`)

### `scaling_server.py` (Compose Daemon / MCP)
- `PROMETHEUS_URL`: Prometheus server URL (default: http://localhost:9090)
- `COMPOSE_PROJECT_NAME`: Docker Compose project name (default: atlas-code)
- `COMPOSE_WORKDIR`: path containing `docker-compose.yml` for scaling commands
- `SCALING_MODE`: `daemon` or `stdio`
- `CHECK_INTERVAL_SEC`: autoscaling loop interval
- `SCALE_COOLDOWN_SEC`: cooldown between automatic scaling actions
- `MIN_REPLICAS` / `MAX_REPLICAS`: scaling guardrails
- `TARGET_SERVICE`: service to scale (repo-level default: `enrichment-system`)
- `LLM_ENABLED`: enable LM Studio-backed reasoning
- `LMSTUDIO_BASE_URL`: LM Studio OpenAI-compatible base URL
- `LMSTUDIO_MODEL`: local model name exposed by LM Studio
- `LMSTUDIO_API_KEY`: optional bearer token if configured in LM Studio
- `LLM_TIMEOUT_SEC`: timeout for model calls
- `LLM_CONFIDENCE_THRESHOLD`: minimum confidence to accept an LLM action
- `LLM_MAX_TOKENS`: response budget for the model call
- `LLM_TEMPERATURE`: keep at `0` for stable operational decisions

Scaling thresholds can be modified at runtime via the HTTP/MCP interfaces or via environment variables at startup.

## LM Studio Integration

Recommended runtime shape:

1. Start a local model in LM Studio with the OpenAI-compatible server enabled.
2. Set `LLM_ENABLED=true`.
3. Point `LMSTUDIO_BASE_URL` to the LM Studio server, for example `http://host.docker.internal:1234/v1`.
4. Set `LMSTUDIO_MODEL` to the exact model identifier exposed by LM Studio.

If the model times out, returns invalid JSON, asks for approval, or proposes an unsafe action, the daemon falls back to the built-in rule-based decision logic.

## Integration with ATLAS

The scaling agent integrates with the broader ATLAS system:

1. **Metrics Collection**: Pulls metrics from Prometheus
2. **Decision Making**: Analyzes load patterns and business requirements
3. **Safe Scaling**: Uses Docker Compose scaling with guardrails and cooldowns
4. **Operational Feedback**: Logs all scaling actions for audit trails

## Return on Agent (ROA) Analysis

The scaling agent provides measurable value:

- **Cost Reduction**: Automatic scaling prevents over-provisioning
- **Performance**: Maintains SLIs during traffic spikes
- **Reliability**: Reduces manual intervention and human error
- **Efficiency**: Optimizes resource utilization

## Safety and Compliance

- **Human-in-the-Loop**: Critical scaling decisions require approval
- **Audit Logging**: All actions are logged with timestamps
- **Rollback Capability**: Failed scaling operations can be reverted
- **Resource Limits**: Prevents runaway scaling scenarios
