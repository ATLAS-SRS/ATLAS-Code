#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="default"
SKIP_DATA_LAYER=false
SKIP_PLT=false
SKIP_APP_LAYER=false
SKIP_LOCUST=false
WAIT_TIMEOUT="300s"
IMMUTABLE_RECOVERY=true
BUILD_IMAGES=false

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_LAYER_DIR="${SCRIPT_DIR}/k8s/data-layer"
PLT_LAYER_DIR="${SCRIPT_DIR}/k8s/plt-layer"
APP_LAYER_DIR="${SCRIPT_DIR}/k8s/app-layer"
DEFAULT_SCALING_AGENT_MANIFEST="${APP_LAYER_DIR}/scaling-agent.yaml"
KIND_SCALING_AGENT_MANIFEST="${APP_LAYER_DIR}/scaling-agent.yaml"
SCALING_AGENT_MANIFEST="${DEFAULT_SCALING_AGENT_MANIFEST}"
SERVICEMONITORS_MANIFEST="${APP_LAYER_DIR}/servicemonitors.yaml"
CLUSTER_PROFILE="unknown"

usage() {
  cat <<'EOF'
Usage: ./deploy.sh [options]

Options:
  -n, --namespace <name>   Kubernetes namespace (default: default)
      --build              Build Docker images before deployment
      --skip-data-layer    Do not install/upgrade Kafka, Redis, Schema Registry
      --skip-plt           Do not install/upgrade Prometheus, Loki, Grafana
      --skip-app-layer     Do not deploy compute layer manifests
      --skip-locust        Do not deploy Locust manifests
      --wait-timeout <d>   Rollout timeout, e.g. 300s (default: 300s)
      --no-immutable-recovery  Disable automatic Kafka StatefulSet immutable field recovery
  -h, --help               Show this help

Examples:
  ./deploy.sh
  ./deploy.sh --namespace fraud-detection
  ./deploy.sh --skip-data-layer
  ./deploy.sh --skip-plt
  ./deploy.sh --skip-locust
EOF
}

log() {
  printf "\n[%s] %s\n" "$(date +"%Y-%m-%d %H:%M:%S")" "$*"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: required command '$1' not found in PATH." >&2
    exit 1
  fi
}

rollout_or_debug() {
  local resource_type="$1"
  local resource_name="$2"
  local label_selector="$3"

  if kubectl rollout status "${resource_type}/${resource_name}" -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"; then
    return 0
  fi

  log "Rollout failed for ${resource_type}/${resource_name}. Collecting diagnostics..."
  kubectl describe "${resource_type}" "${resource_name}" -n "$NAMESPACE" >&2 || true

  local pods
  pods=$(kubectl get pods -n "$NAMESPACE" -l "$label_selector" -o name 2>/dev/null || true)
  if [[ -n "$pods" ]]; then
    for pod in $pods; do
      echo "--- ${pod}" >&2
      kubectl describe -n "$NAMESPACE" "$pod" >&2 || true
      kubectl logs -n "$NAMESPACE" "$pod" --tail=120 >&2 || true
      kubectl logs -n "$NAMESPACE" "$pod" --previous --tail=120 >&2 || true
    done
  fi

  exit 1
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -n|--namespace)
        NAMESPACE="$2"
        shift 2
        ;;
      --build)
        BUILD_IMAGES=true
        shift
        ;;
      --skip-data-layer)
        SKIP_DATA_LAYER=true
        shift
        ;;
      --skip-plt)
        SKIP_PLT=true
        shift
        ;;
      --skip-app-layer)
        SKIP_APP_LAYER=true
        shift
        ;;
      --skip-locust)
        SKIP_LOCUST=true
        shift
        ;;
      --wait-timeout)
        WAIT_TIMEOUT="$2"
        shift 2
        ;;
      --no-immutable-recovery)
        IMMUTABLE_RECOVERY=false
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "ERROR: unknown option '$1'" >&2
        usage
        exit 1
        ;;
    esac
  done
}

ensure_namespace() {
  if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
    log "Creating namespace ${NAMESPACE}"
    kubectl create namespace "$NAMESPACE"
  fi
}

service_monitor_crd_exists() {
  kubectl get crd servicemonitors.monitoring.coreos.com >/dev/null 2>&1
}

detect_cluster_profile() {
  local current_context

  current_context="$(kubectl config current-context 2>/dev/null || true)"

  case "$current_context" in
    kind-*)
      CLUSTER_PROFILE="kind"
      SCALING_AGENT_MANIFEST="$KIND_SCALING_AGENT_MANIFEST"
      ;;
    docker-desktop|docker-desktop-*)
      CLUSTER_PROFILE="docker-desktop"
      SCALING_AGENT_MANIFEST="$DEFAULT_SCALING_AGENT_MANIFEST"
      ;;
    *)
      CLUSTER_PROFILE="generic"
      SCALING_AGENT_MANIFEST="$DEFAULT_SCALING_AGENT_MANIFEST"
      ;;
  esac

  if [[ "$CLUSTER_PROFILE" == "kind" && ! -f "$SCALING_AGENT_MANIFEST" ]]; then
    log "Kind manifest not found at ${SCALING_AGENT_MANIFEST}, falling back to ${DEFAULT_SCALING_AGENT_MANIFEST}"
    SCALING_AGENT_MANIFEST="$DEFAULT_SCALING_AGENT_MANIFEST"
  fi

  log "Detected cluster profile ${CLUSTER_PROFILE} (context: ${current_context:-unknown})"
  log "Using scaling-agent manifest ${SCALING_AGENT_MANIFEST}"
}

build_images() {
  log "Building Docker images"

  docker build -t atlas/api-gateway:latest -f gateway/Dockerfile gateway/
  docker build -t atlas/enrichment-system:latest -f enrichment-system/Dockerfile enrichment-system/
  docker build -t atlas/scoring-system:fix-readiness-2 -f scoring-system/Dockerfile scoring-system/
  docker build -t atlas/notification-system:latest -f notification-system/Dockerfile notification-system/
  docker build -t atlas/agent-guardian:latest -f agents-devops/Dockerfile agents-devops/
  docker build -t atlas/kafka-connect:latest -f Dockerfile.connect .

  log "Docker images built successfully"
}

deploy_data_layer() {
  log "Deploying data layer (Kafka, Redis, Schema Registry)"

  require_cmd helm

  helm repo add bitnami https://charts.bitnami.com/bitnami >/dev/null 2>&1 || true
  helm repo update >/dev/null

  local kafka_helm_args=()
  local redis_helm_args=()
  local postgres_helm_args=()

  if ! service_monitor_crd_exists; then
    log "ServiceMonitor CRD not found; disabling chart metrics until monitoring CRDs are installed"
    kafka_helm_args+=(--set "metrics.enabled=false" --set "metrics.kafkaExporter.enabled=false")
    redis_helm_args+=(--set "metrics.enabled=false" --set "metrics.serviceMonitor.enabled=false")
    postgres_helm_args+=(--set "metrics.enabled=false" --set "metrics.serviceMonitor.enabled=false")
  fi

  local kafka_output=""
  if ! kafka_output=$(helm upgrade --install atlas-kafka bitnami/kafka \
      --namespace "$NAMESPACE" \
      --create-namespace \
      "${kafka_helm_args[@]}" \
      -f "${DATA_LAYER_DIR}/kafka-values.yaml" 2>&1); then
    if [[ "$IMMUTABLE_RECOVERY" == true ]] && [[ "$kafka_output" == *"Forbidden: updates to statefulset spec"* ]]; then
      log "Kafka upgrade failed due to immutable StatefulSet fields. Applying safe recovery."
      kubectl delete statefulset atlas-kafka-controller -n "$NAMESPACE" --ignore-not-found

      helm upgrade --install atlas-kafka bitnami/kafka \
        --namespace "$NAMESPACE" \
        --create-namespace \
        "${kafka_helm_args[@]}" \
        -f "${DATA_LAYER_DIR}/kafka-values.yaml"
    else
      echo "$kafka_output" >&2
      kubectl delete job kafka-connector-setup -n "$NAMESPACE" --ignore-not-found
      exit 1
    fi
  fi

  helm upgrade --install atlas-redis bitnami/redis \
    --namespace "$NAMESPACE" \
    --create-namespace \
    "${redis_helm_args[@]}" \
    -f "${DATA_LAYER_DIR}/redis-values.yaml"

  kubectl apply -n "$NAMESPACE" -f "${DATA_LAYER_DIR}/postgres-init-configmap.yaml"

  helm upgrade --install atlas-postgres bitnami/postgresql \
    --namespace "$NAMESPACE" \
    --create-namespace \
    --set "auth.postgresPassword=${POSTGRES_PASSWORD}" \
    "${postgres_helm_args[@]}" \
    -f "${DATA_LAYER_DIR}/postgres-values.yaml"

  kubectl apply -n "$NAMESPACE" -f "${DATA_LAYER_DIR}/schema-registry.yaml"
  kubectl apply -n "$NAMESPACE" -f "${DATA_LAYER_DIR}/kafka-connect.yaml"

  log "Waiting for data layer rollouts"
  kubectl rollout status statefulset/atlas-kafka-controller -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"
  kubectl rollout status statefulset/atlas-redis-master -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"
  kubectl rollout status deploy/schema-registry -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"
  kubectl rollout status statefulset/atlas-postgres-postgresql -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"
  kubectl rollout status deploy/kafka-connect -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"
  
  kubectl delete job kafka-connector-setup -n "$NAMESPACE" --ignore-not-found
  
  cat <<EOF | kubectl apply -n "$NAMESPACE" -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: kafka-connector-sink-config
data:
  sink-config.json: |
    {
      "connector.class": "io.confluent.connect.jdbc.JdbcSinkConnector",
      "tasks.max": "1",
      "topics": "scored-transactions",
      "connection.url": "jdbc:postgresql://atlas-postgres-postgresql.default.svc.cluster.local:5432/atlas_db",
      "connection.user": "postgres",
      "connection.password": "${POSTGRES_PASSWORD}",
      "table.name.format": "transactions_history",
      "insert.mode": "upsert",
      "pk.mode": "record_value",
      "pk.fields": "transaction_id",
      "key.converter": "org.apache.kafka.connect.storage.StringConverter",
      "value.converter": "io.confluent.connect.avro.AvroConverter",
      "value.converter.schema.registry.url": "http://schema-registry.default.svc.cluster.local:8081",
      "auto.create": "false",
      "auto.evolve": "false"
    }
EOF

  kubectl apply -n "$NAMESPACE" -f "${DATA_LAYER_DIR}/kafka-connector-setup-job.yaml"
  kubectl wait --for=condition=complete job/kafka-connector-setup -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"
  
  log "Data layer deployed"
}

deploy_app_layer() {
  log "Deploying app layer manifests"

  kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/config.yaml"
  kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/deployments.yaml"
  kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/services.yaml"
  kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/ingress.yaml"

  if [[ -f "$SERVICEMONITORS_MANIFEST" ]] && service_monitor_crd_exists; then
    kubectl apply -n "$NAMESPACE" -f "$SERVICEMONITORS_MANIFEST"
  elif [[ -f "$SERVICEMONITORS_MANIFEST" ]]; then
    log "ServiceMonitor CRD not available; skipping ${SERVICEMONITORS_MANIFEST}"
  fi

  log "Waiting for app rollouts"
  rollout_or_debug deployment api-gateway "app.kubernetes.io/name=api-gateway"
  rollout_or_debug deployment enrichment-system "app.kubernetes.io/name=enrichment-system"
  rollout_or_debug deployment scoring-system "app.kubernetes.io/name=scoring-system"
  rollout_or_debug deployment notification-system "app.kubernetes.io/name=notification-system"

  if [[ -f "$SCALING_AGENT_MANIFEST" ]]; then
    log "Deploying scaling-agent"
    kubectl apply -n "$NAMESPACE" -f "$SCALING_AGENT_MANIFEST"
    rollout_or_debug deployment atlas-aiops-agent "app.kubernetes.io/name=atlas-aiops-agent"
  else
    log "Scaling-agent manifest not found at ${SCALING_AGENT_MANIFEST}, skipping"
  fi

  log "App layer deployed"
}

deploy_plt() {
  log "Deploying PLT Stack (Prometheus, Loki, Grafana)"

  require_cmd helm

  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo update >/dev/null

  helm upgrade --install atlas-loki grafana/loki-stack \
    --namespace "$NAMESPACE" \
    --create-namespace \
    --set loki.isDefault=false \
    --set grafana.sidecar.datasources.enabled=false

  helm upgrade --install atlas-monitoring prometheus-community/kube-prometheus-stack \
    --namespace "$NAMESPACE" \
    --create-namespace \
    --set "grafana.adminPassword=${GRAFANA_ADMIN_PASSWORD}" \
    -f "${PLT_LAYER_DIR}/grafana-values.yaml" \
    -f "${PLT_LAYER_DIR}/prometheus-values.yaml"

  kubectl apply -f "${PLT_LAYER_DIR}/prometheus-rules.yaml" -n "$NAMESPACE"

  log "Waiting for PLT rollouts"
  kubectl rollout status deploy/atlas-monitoring-grafana -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"

  local prom_sts=""
  prom_sts=$(kubectl get statefulset -n "$NAMESPACE" \
    -l "release=atlas-monitoring,app.kubernetes.io/name=prometheus" \
    -o name | head -n 1 | sed 's|.*/||')

  if [[ -z "$prom_sts" ]]; then
    prom_sts=$(kubectl get statefulset -n "$NAMESPACE" \
      -l "app.kubernetes.io/name=prometheus" \
      -o name | head -n 1 | sed 's|.*/||')
  fi

  if [[ -z "$prom_sts" ]]; then
    echo "ERROR: Prometheus StatefulSet not found for release 'atlas-monitoring'." >&2
    kubectl get statefulset -n "$NAMESPACE" >&2 || true
    exit 1
  fi

  kubectl rollout status "statefulset/${prom_sts}" -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"

  log "PLT stack deployed"
}

deploy_grafana_mcp() {
  log "Deploying Grafana MCP Server"

  require_cmd curl
  require_cmd jq

  kubectl rollout status deploy/atlas-monitoring-grafana -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"

  kubectl port-forward -n "$NAMESPACE" svc/atlas-monitoring-grafana 3000:80 >/dev/null 2>&1 &
  local pf_pid=$!

  cleanup_port_forward() {
    if [[ -n "${pf_pid:-}" ]]; then
      kill "$pf_pid" >/dev/null 2>&1 || true
    fi
  }
  trap cleanup_port_forward RETURN

  local grafana_api_url="http://admin:${GRAFANA_ADMIN_PASSWORD}@127.0.0.1:3000/api"
  local sa_id=""

  for _ in $(seq 1 20); do
    if sa_id=$(curl -fsS "$grafana_api_url/serviceaccounts/search?query=mcp-server" | jq -r '.serviceAccounts[0].id // empty'); then
      if [[ -n "$sa_id" ]]; then
        break
      fi
    fi
    sleep 2
  done

  if [[ -z "$sa_id" ]]; then
    log "Creating new Grafana service account mcp-server"
    sa_id=$(curl -fsS -X POST "$grafana_api_url/serviceaccounts" \
      -H "Content-Type: application/json" \
      -d '{"name":"mcp-server", "role": "Admin"}' | jq -r '.id')
  fi

  if [[ -z "$sa_id" ]]; then
    echo "ERROR: unable to create or locate Grafana service account mcp-server" >&2
    exit 1
  fi

  log "Generating Grafana MCP token"
  local new_token
  local token_payload
  token_payload=$(printf '{"name":"mcp-token-%s"}' "$(date +%s)")
  new_token=$(curl -fsS -X POST "$grafana_api_url/serviceaccounts/$sa_id/tokens" \
    -H "Content-Type: application/json" \
    -d "$token_payload" | jq -r '.key')

  if [[ -z "$new_token" || "$new_token" == "null" ]]; then
    echo "ERROR: unable to generate Grafana MCP token" >&2
    exit 1
  fi

  kubectl create secret generic grafana-mcp-credentials \
    --namespace "$NAMESPACE" \
    --from-literal=token="$new_token" \
    --dry-run=client -o yaml | kubectl apply -f -

  kubectl apply -n "$NAMESPACE" -f "${PLT_LAYER_DIR}/grafana-mcp.yaml"
  rollout_or_debug deployment grafana-mcp-server "app=grafana-mcp-server"

  log "Grafana MCP Server deployed"
}

deploy_ingress_controller() {
  log "Setting up NGINX Ingress Controller"

  require_cmd helm

  helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx >/dev/null 2>&1 || true
  helm repo update >/dev/null

  helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
    --namespace ingress-nginx \
    --create-namespace \
    --wait

  log "NGINX Ingress Controller is ready"
}

deploy_locust() {
  log "Deploying Locust manifests"

  kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/locust-configmap.yaml"
  kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/deployments.yaml"
  kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/services.yaml"
  if [[ -f "${APP_LAYER_DIR}/ingress.yaml" ]]; then
    kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/ingress.yaml"
  fi

  log "Waiting for Locust rollouts"
  kubectl rollout status deploy/locust-master -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"
  kubectl rollout status deploy/locust-worker -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"

  log "Locust deployed"
}

print_summary() {
  log "Deployment summary"
  kubectl get pods -n "$NAMESPACE" -o wide
  echo
  kubectl get svc -n "$NAMESPACE"
  echo
  kubectl get ingress -n "$NAMESPACE" || true

  cat <<EOF

Quick checks:
  kubectl get pods -n ${NAMESPACE}
  kubectl logs -n ${NAMESPACE} deploy/api-gateway --tail=100
  kubectl logs -n ${NAMESPACE} deploy/enrichment-system --tail=100
  kubectl logs -n ${NAMESPACE} deploy/scoring-system --tail=100
  kubectl logs -n ${NAMESPACE} deploy/notification-system --tail=100
  kubectl logs -n ${NAMESPACE} deploy/atlas-aiops-agent --tail=100

Endpoints (tramite NGINX Ingress & nip.io):
  API Gateway:   http://fraud-api.127.0.0.1.nip.io
  Locust UI:     http://locust.127.0.0.1.nip.io
  Grafana UI:    http://grafana.127.0.0.1.nip.io
  Prometheus:    http://prometheus.127.0.0.1.nip.io

Credenziali Grafana:
  User: admin
  Password: ${GRAFANA_ADMIN_PASSWORD}

Credenziali PostgreSQL:
  User: postgres
  Password: ${POSTGRES_PASSWORD}
  Database: atlas_db

EOF
}

main() {
  parse_args "$@"

  if [[ -z "${GRAFANA_ADMIN_PASSWORD:-}" ]]; then
    GRAFANA_ADMIN_PASSWORD="admin"
    log "GRAFANA_ADMIN_PASSWORD not set; using default local value 'admin'"
  fi

  if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then
    POSTGRES_PASSWORD="postgres"
    log "POSTGRES_PASSWORD not set; using default local value 'postgres'"
  fi

  require_cmd kubectl

  log "Current context: $(kubectl config current-context)"
  kubectl get nodes >/dev/null
  detect_cluster_profile

  ensure_namespace

  deploy_ingress_controller

  if [[ "$NAMESPACE" != "default" ]]; then
    log "WARNING: app-layer ConfigMap currently references *.default.svc.cluster.local endpoints."
    log "If deploying outside 'default', update k8s/app-layer/config.yaml accordingly."
  fi

  if [[ "$SKIP_PLT" == false ]]; then
    deploy_plt
    deploy_grafana_mcp
  else
    log "Skipping PLT deployment"
  fi

  if [[ "$BUILD_IMAGES" == true ]]; then
    build_images
  fi

  if [[ "$SKIP_DATA_LAYER" == false ]]; then
    deploy_data_layer
  else
    log "Skipping data layer deployment"
  fi

  if [[ "$SKIP_APP_LAYER" == false ]]; then
    deploy_app_layer
  else
    log "Skipping app layer deployment"
  fi

  if [[ "$SKIP_LOCUST" == false ]]; then
    deploy_locust
  else
    log "Skipping Locust deployment"
  fi

  print_summary
}

main "$@"
