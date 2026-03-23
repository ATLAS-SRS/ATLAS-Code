# ATLAS Scaling Agent

An autoscaling service for the ATLAS fraud detection system with two operator-facing modes:
- `daemon`: HTTP service plus internal autoscaling loop for production/runtime use
- `stdio`: MCP server for manual operator workflows

## Overview

The Scaling Agent monitors system metrics (CPU, memory, request rates) and automatically scales the `kafka-consumer` service based on configurable thresholds. It operates within safety boundaries and provides operational visibility.

When `LLM_ENABLED=true`, the daemon can consult a local model served by LM Studio through its OpenAI-compatible API. The model acts as the primary operational recommender, while the daemon still enforces cooldowns, replica guardrails, and fallback logic.

## Features

- **Real-time Monitoring**: Tracks CPU, memory, and request rate metrics
- **Intelligent Scaling**: Automatically scales up/down based on load
- **Safety Boundaries**: Configurable thresholds with cooldown periods
- **MCP Integration**: Exposes scaling capabilities via Model Context Protocol
- **Operational Safety**: Human-in-the-loop for critical scaling decisions
- **LM Studio Support**: Optional local LLM reasoning with deterministic fallback

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

## Scaling Logic

### Scale Up Conditions
- CPU usage > 70%
- Memory usage > 80%
- Request rate > 100/min

### Scale Down Conditions
- All metrics < 30% of thresholds
- Minimum 1 replica maintained

### Safety Features
- 5-minute cooldown between scaling actions
- Maximum 5 replicas limit
- Graceful error handling

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
- `scale_kafka_consumer`: Scale the kafka-consumer service
- `get_scaling_thresholds`: View current scaling thresholds
- `update_scaling_thresholds`: Modify scaling thresholds

## Usage

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

Environment variables:
- `PROMETHEUS_URL`: Prometheus server URL (default: http://localhost:9090)
- `COMPOSE_PROJECT_NAME`: Docker Compose project name (default: atlas-code)
- `COMPOSE_WORKDIR`: path containing `docker-compose.yml` for scaling commands
- `SCALING_MODE`: `daemon` or `stdio`
- `CHECK_INTERVAL_SEC`: autoscaling loop interval
- `SCALE_COOLDOWN_SEC`: cooldown between automatic scaling actions
- `MIN_REPLICAS` / `MAX_REPLICAS`: scaling guardrails
- `TARGET_SERVICE`: service to scale (default: `kafka-consumer`)
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
