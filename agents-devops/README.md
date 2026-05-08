# ATLAS Scaling Agent

The `agents-devops/` directory contains the ATLAS Guardian runtime for alert intake, scaling orchestration, and operational remediation:
- `main_agent_guardian.py`: FastAPI webhook for alert intake and remediation orchestration
- `k8s_mcp.py`: MCP tool server exposing Prometheus and Kubernetes operations over stdio

## Overview

### Kubernetes Central Orchestrator (`main_agent_guardian.py`)

This runtime accepts Alertmanager webhooks and executes a reasoning loop for remediation:
- Receives firing alerts via FastAPI webhook on port 8000
- Reads global system state from Prometheus via MCP tool calls
- Exposes MCP tools (`get_rps`, `get_current_replicas`, `set_replicas`, `get_scaling_recommendation`, `get_workload_health`, and budget-aware tools) with strict typing
- Uses LangGraph to model the alert remediation workflow as explicit states and transitions
- Uses OpenAI-compatible tool-calling to run an autonomous ReAct loop for each alert
- Applies scaling and remediation actions with Kubernetes Deployment scale API
- Enforces a global replica budget so scaling actions never exceed `TOTAL_REPLICA_BUDGET`
- Deduplicates alerts with `ALERT_DEDUP_WINDOW_SECONDS` (default 60s) and serializes concurrent runs per deployment

Default target list:
- `api-gateway,scoring-system,enrichment-system,notification-system`

### Local MCP Tool Server (`k8s_mcp.py`)

The tool server exposes Prometheus and Kubernetes operations used by the Guardian workflow. It is started locally by `main_agent_guardian.py` over stdio.

## Features

- **Feed-Forward Pipeline Scaling**: One global ingress load signal drives sequential scaling decisions across multiple microservices
- **Kubernetes Orchestration**: Native Deployment scale operations for app-layer services
- **Real-time Monitoring**: Prometheus-backed load observations
- **Safety Boundaries**: Hard guardrails enforced at tool level (`MIN_REPLICAS`, `MAX_REPLICAS`)
- **Replica Budget**: Global scaling actions are blocked when the projected total replica usage exceeds `TOTAL_REPLICA_BUDGET`
- **MCP Integration**: `mcp_client.py` uses MCP-defined tools for autonomous execution; `mcp_server.py` exposes the local tool surface in `stdio` mode
- **Operational Safety**: Human-in-the-loop and auditable scaling actions
- **LM Studio Support**: Optional local LLM reasoning with deterministic fallback paths
- **Budget Allocation Tools**: The runtime also exposes `get_hpa_limits`, `get_budget_state`, `plan_budget_allocation`, `execute_budget_allocation`, `set_hpa_max_replicas`, `restore_cpu_limits`, and `get_workload_health`

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

## Guardian Agent Loop Logic (`main_agent_guardian.py`)

1. Accept Alertmanager webhook POST with firing alerts
2. Parse alert payload and extract deployment/severity context
3. Check `ALERT_DEDUP_WINDOW_SECONDS` to skip duplicate alerts
4. Acquire per-deployment lock to serialize concurrent alert handling
5. Build an OpenAI-compatible client pointing to `LLM_API_URL`
6. Load the MCP tools and convert them into OpenAI tool schemas
7. Build a LangGraph state machine for alert remediation
8. Invoke the graph with the alert context and system prompt
9. While the model emits `tool_calls`, execute the matching local tool and append the result
10. When the model stops requesting tools or step budget exhausted, record the incident report
11. Upsert incident reports by `incident_id` and store `post_mortem_path` for dashboard access

Default RPS query:
- `sum(rate(http_requests_total{job="api-gateway"}[1m]))`

## MCP Tools

The local tool server exposes the following MCP tools:

- `get_rps`: read the global RPS from Prometheus
- `get_current_replicas`: read deployment replica count from Kubernetes
- `set_replicas`: update deployment replicas within guardrails
- `get_scaling_recommendation`: combine current load and replica state into a target action

## Usage

### Guardian Agent Runtime

This is the runtime used by the Kubernetes manifest (`k8s/app-layer/agent-guardian.yaml`):

```bash
python -u main_agent_guardian.py
```

Main environment variables:
- `PROMETHEUS_URL`
- `NAMESPACE`
- `TARGET_DEPLOYMENTS`
- `PROMQL_RPS_QUERY`
- `CHECK_INTERVAL`
- `MIN_REPLICAS` / `MAX_REPLICAS`
- `BUDGET_MAX_REPLICAS`: per-deployment ceiling used to derive the default global budget
- `TOTAL_REPLICA_BUDGET`: maximum allowed replica sum across all target deployments
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
- `BUDGET_MAX_REPLICAS`: per-deployment ceiling used to derive the default global budget
- `TOTAL_REPLICA_BUDGET`: maximum allowed replica sum across all target deployments
- `EMERGENCY_MAX_REPLICAS`: absolute upper bound for emergency actions, clamped to the configured budget

### `k8s_mcp.py` (MCP Tool Server)
- `PROMETHEUS_URL`: Prometheus server URL (default: http://localhost:9090)
- `NAMESPACE`: Kubernetes namespace used by the tool server
- `TARGET_DEPLOYMENTS`: comma-separated deployment names that can be evaluated
- `PROMQL_RPS_QUERY`: global ingress RPS query (default targets `api-gateway`)
- `MIN_REPLICAS` / `MAX_REPLICAS`: scaling guardrails enforced by tool validation
- `BUDGET_MAX_REPLICAS`: per-deployment ceiling used to derive the default global budget
- `TOTAL_REPLICA_BUDGET`: maximum allowed replica sum across all target deployments
- `EMERGENCY_MAX_REPLICAS`: absolute upper bound for emergency actions
## LM Studio Integration

Recommended runtime shape:

1. Start a local model in LM Studio with the OpenAI-compatible server enabled.
2. Point `LLM_API_URL` or `LM_STUDIO_URL` to the LM Studio server, for example `http://host.docker.internal:1234/v1`.
3. Set `LLM_MODEL` or `LMSTUDIO_MODEL` to the exact model identifier exposed by LM Studio.

If the model times out, returns invalid JSON, or proposes an unsafe action, the Guardian falls back to a safe default.

## Safety and Compliance

- **Human-in-the-Loop**: Critical scaling decisions require approval
- **Audit Logging**: All actions are logged with timestamps
- **Rollback Capability**: Failed scaling operations can be reverted
- **Resource Limits**: Prevents runaway scaling scenarios
