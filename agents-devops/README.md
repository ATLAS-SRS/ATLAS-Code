# ATLAS Orchestrator

The `agents-orchestrator/` directory contains the current ATLAS scaling runtime:
- `mcp_client.py`: LangGraph-driven Kubernetes orchestrator (single-loop, multi-deployment)
- `mcp_server.py`: local MCP server exposing Prometheus/Kubernetes tools over stdio

## Overview

### Kubernetes Central Orchestrator (`mcp_client.py`)

This runtime implements a feed-forward autoscaling model for the full fraud pipeline:
- Reads global ingress load from API Gateway once per cycle using Prometheus RPS
- Exposes internal MCP tools (`get_rps`, `get_current_replicas`, `set_replicas`) with strict typing and descriptive docstrings
- Exposes internal MCP tools (`get_rps`, `get_current_replicas`, `set_replicas`, `get_scaling_recommendation`) with strict typing and descriptive docstrings
- Uses LangGraph to model the per-deployment reasoning loop as explicit states and transitions
- Uses OpenAI-compatible tool-calling (LM Studio / Ollama OpenAI API) to run an autonomous ReAct loop per deployment
- Iterates deployments sequentially (`TARGET_DEPLOYMENTS`) in a single loop
- Executes tool calls until the assistant returns a final answer without additional tool calls
- Applies replica updates with Kubernetes Deployment scale API with hard guardrails (`MIN_REPLICAS`, `MAX_REPLICAS`)

Default target list:
- `api-gateway,scoring-system,enrichment-system,notification-system`

### Local MCP Tool Server (`mcp_server.py`)

The tool server exposes the Prometheus and Kubernetes operations used by the orchestrator. It is started locally by `mcp_client.py` over stdio.

## Features

- **Feed-Forward Pipeline Scaling**: One global ingress load signal drives sequential scaling decisions across multiple microservices
- **Kubernetes Orchestration**: Native Deployment scale operations for app-layer services
- **Real-time Monitoring**: Prometheus-backed load observations
- **Safety Boundaries**: Hard guardrails enforced at tool level (`MIN_REPLICAS`, `MAX_REPLICAS`)
- **MCP Integration**: `mcp_client.py` uses MCP-defined tools for autonomous execution; `mcp_server.py` exposes the local tool surface in `stdio` mode
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

## Kubernetes Central Loop Logic (`mcp_client.py`)

1. Parse `TARGET_DEPLOYMENTS` into the allowed deployment set.
2. Build an OpenAI-compatible client pointing to `LLM_API_URL`.
3. Load the MCP tools and convert them into OpenAI tool schemas.
4. Build a LangGraph state machine for a single deployment cycle.
5. For each deployment in order, invoke the graph with the system prompt and user prompt.
6. While the model emits `tool_calls`, execute the matching local tool and append the result as a tool message.
7. When the model stops requesting tools or the step budget is exhausted, record the final operational report.
8. Sleep 2 seconds between per-deployment iterations and `CHECK_INTERVAL` after a full pass.

Default RPS query:
- `sum(rate(http_requests_total{job="api-gateway"}[1m]))`

## MCP Tools

The local tool server exposes the following MCP tools:

- `get_rps`: read the global RPS from Prometheus
- `get_current_replicas`: read deployment replica count from Kubernetes
- `set_replicas`: update deployment replicas within guardrails
- `get_scaling_recommendation`: combine current load and replica state into a target action

## Usage

### Kubernetes Central Orchestrator

This is the runtime used by the Kubernetes manifest (`k8s/app-layer/agent-guardian.yaml`):

```bash
python -u mcp_client.py
```

Main environment variables:
- `PROMETHEUS_URL`
- `NAMESPACE`
- `TARGET_DEPLOYMENTS`
- `PROMQL_RPS_QUERY`
- `CHECK_INTERVAL`
- `MIN_REPLICAS` / `MAX_REPLICAS`
- `LLM_API_URL` / `LM_STUDIO_URL` / `LLM_MODEL` / `LMSTUDIO_MODEL`
- `GRAFANA_MCP_URL`
- `MAX_TOOL_STEPS`
- `RPS_REPLICA_THRESHOLDS`

### Manual Operation
```bash
# Run the MCP server for operator tooling
python mcp_server.py
```

## Tests

Repository tests, when present, live alongside this package.

Run them from the repo root:

```bash
docker compose run --rm --entrypoint pytest agent-guardian -o cache_dir=/tmp/pytest-cache /workspace/agents-orchestrator/tests
```

## Configuration

### `mcp_client.py` (Kubernetes Central Orchestrator)
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

### `mcp_server.py` (Local MCP Tool Server)
- `PROMETHEUS_URL`: Prometheus server URL (default: http://localhost:9090)
- `NAMESPACE`: Kubernetes namespace used by the tool server
- `TARGET_DEPLOYMENTS`: comma-separated deployment names that can be evaluated
- `PROMQL_RPS_QUERY`: global ingress RPS query (default targets `api-gateway`)
- `MIN_REPLICAS` / `MAX_REPLICAS`: scaling guardrails enforced by tool validation
- `RPS_REPLICA_THRESHOLDS`: 4 ascending comma-separated RPS thresholds used to map traffic into a 1..5 target replica band

Scaling thresholds can be modified at runtime via the HTTP/MCP interfaces or via environment variables at startup.

## LM Studio Integration

Recommended runtime shape:

1. Start a local model in LM Studio with the OpenAI-compatible server enabled.
2. Point `LLM_API_URL` or `LM_STUDIO_URL` to the LM Studio server, for example `http://host.docker.internal:1234/v1`.
3. Set `LLM_MODEL` or `LMSTUDIO_MODEL` to the exact model identifier exposed by LM Studio.

If the model times out, returns invalid JSON, or proposes an unsafe action, the orchestrator falls back to HOLD for safety.

## Integration with ATLAS

The scaling orchestrator integrates with the broader ATLAS system:

1. **Metrics Collection**: Pulls metrics from Prometheus
2. **Decision Making**: Analyzes load patterns and business requirements
3. **Safe Scaling**: Uses Docker Compose scaling with guardrails and cooldowns
4. **Operational Feedback**: Logs all scaling actions for audit trails

## Return on Agent (ROA) Analysis

The scaling orchestrator provides measurable value:

- **Cost Reduction**: Automatic scaling prevents over-provisioning
- **Performance**: Maintains SLIs during traffic spikes
- **Reliability**: Reduces manual intervention and human error
- **Efficiency**: Optimizes resource utilization

## Safety and Compliance

- **Human-in-the-Loop**: Critical scaling decisions require approval
- **Audit Logging**: All actions are logged with timestamps
- **Rollback Capability**: Failed scaling operations can be reverted
- **Resource Limits**: Prevents runaway scaling scenarios
