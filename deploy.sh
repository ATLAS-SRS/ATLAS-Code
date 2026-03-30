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
SCALING_AGENT_MANIFEST="${APP_LAYER_DIR}/scaling-agent.yaml"
SERVICEMONITORS_MANIFEST="${APP_LAYER_DIR}/servicemonitors.yaml"

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

build_images() {
  log "Building Docker images"

  docker build -t atlas/api-gateway:latest -f gateway/Dockerfile gateway/
  docker build -t atlas/enrichment-system:latest -f enrichment-system/Dockerfile enrichment-system/
  docker build -t atlas/scoring-system:fix-readiness-2 -f scoring-system/Dockerfile scoring-system/
  docker build -t atlas/notification-system:latest -f notification-system/Dockerfile notification-system/
  docker build -t atlas/scaling-agent:latest -f scaling-agent/Dockerfile scaling-agent/

  log "Docker images built successfully"
}

deploy_data_layer() {
  log "Deploying data layer (Kafka, Redis, Schema Registry)"

  require_cmd helm

  helm repo add bitnami https://charts.bitnami.com/bitnami >/dev/null 2>&1 || true
  helm repo update >/dev/null

  local kafka_output=""
  if ! kafka_output=$(helm upgrade --install atlas-kafka bitnami/kafka \
      --namespace "$NAMESPACE" \
      --create-namespace \
      -f "${DATA_LAYER_DIR}/kafka-values.yaml" 2>&1); then
    if [[ "$IMMUTABLE_RECOVERY" == true ]] && [[ "$kafka_output" == *"Forbidden: updates to statefulset spec"* ]]; then
      log "Kafka upgrade failed due to immutable StatefulSet fields. Applying safe recovery."
      kubectl delete statefulset atlas-kafka-controller -n "$NAMESPACE" --ignore-not-found

      helm upgrade --install atlas-kafka bitnami/kafka \
        --namespace "$NAMESPACE" \
        --create-namespace \
        -f "${DATA_LAYER_DIR}/kafka-values.yaml"
    else
      echo "$kafka_output" >&2
      exit 1
    fi
  fi

  helm upgrade --install atlas-redis bitnami/redis \
    --namespace "$NAMESPACE" \
    --create-namespace \
    -f "${DATA_LAYER_DIR}/redis-values.yaml"

  kubectl apply -n "$NAMESPACE" -f "${DATA_LAYER_DIR}/schema-registry.yaml"

  log "Waiting for data layer rollouts"
  kubectl rollout status statefulset/atlas-kafka-controller -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"
  kubectl rollout status statefulset/atlas-redis-master -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"
  kubectl rollout status deploy/schema-registry -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"

  log "Data layer deployed"
}

deploy_app_layer() {
  log "Deploying app layer manifests"

  kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/config.yaml"
  kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/deployments.yaml"
  kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/services.yaml"
  kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/ingress.yaml"

  if [[ -f "$SERVICEMONITORS_MANIFEST" ]]; then
    kubectl apply -n "$NAMESPACE" -f "$SERVICEMONITORS_MANIFEST"
  fi

  log "Waiting for app rollouts"
  rollout_or_debug deployment api-gateway "app.kubernetes.io/name=api-gateway"
  rollout_or_debug deployment enrichment-system "app.kubernetes.io/name=enrichment-system"
  rollout_or_debug deployment scoring-system "app.kubernetes.io/name=scoring-system"
  rollout_or_debug deployment notification-system "app.kubernetes.io/name=notification-system"

  if [[ -f "$SCALING_AGENT_MANIFEST" ]]; then
    log "Deploying scaling-agent"
    kubectl apply -n "$NAMESPACE" -f "$SCALING_AGENT_MANIFEST"
    rollout_or_debug deployment scaling-agent "app=scaling-agent"
  else
    log "Scaling agent manifest not found at ${SCALING_AGENT_MANIFEST}, skipping"
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

  kubectl apply -n "$NAMESPACE" -f "${DATA_LAYER_DIR}/locust-configmap.yaml"
  kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/deployments.yaml"
  kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/services.yaml"
  # Aggiunta dell'Ingress per Locust
  kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/locust-ingress.yaml"

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
  kubectl logs -n ${NAMESPACE} deploy/scaling-agent --tail=100

Endpoints (tramite NGINX Ingress & nip.io):
  API Gateway:   http://fraud-api.127.0.0.1.nip.io
  Locust UI:     http://locust.127.0.0.1.nip.io
  Grafana UI:    http://grafana.127.0.0.1.nip.io
  Prometheus:    http://prometheus.127.0.0.1.nip.io

Credenziali Grafana:
  User: admin
  Pass: \$(kubectl get secret -n ${NAMESPACE} atlas-monitoring-grafana -o jsonpath="{.data.admin-password}" 2>/dev/null | base64 --decode)
EOF
}

main() {
  parse_args "$@"

  if [[ -z "${GRAFANA_ADMIN_PASSWORD:-}" ]]; then
    echo "ERROR: GRAFANA_ADMIN_PASSWORD non impostata o vuota. Esporta la variabile prima di eseguire ./deploy.sh." >&2
    exit 1
  fi

  require_cmd kubectl

  log "Current context: $(kubectl config current-context)"
  kubectl get nodes >/dev/null

  ensure_namespace

  deploy_ingress_controller

  if [[ "$NAMESPACE" != "default" ]]; then
    log "WARNING: app-layer ConfigMap currently references *.default.svc.cluster.local endpoints."
    log "If deploying outside 'default', update k8s/app-layer/config.yaml accordingly."
  fi

  if [[ "$BUILD_IMAGES" == true ]]; then
    build_images
  fi

  if [[ "$SKIP_DATA_LAYER" == false ]]; then
    deploy_data_layer
  else
    log "Skipping data layer deployment"
  fi

  if [[ "$SKIP_PLT" == false ]]; then
    deploy_plt
  else
    log "Skipping PLT deployment"
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
