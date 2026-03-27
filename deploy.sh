#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="default"
SKIP_DATA_LAYER=false
SKIP_APP_LAYER=false
SKIP_LOCUST=false
WAIT_TIMEOUT="300s"
IMMUTABLE_RECOVERY=true
BUILD_IMAGES=false

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_LAYER_DIR="${SCRIPT_DIR}/k8s/data-layer"
APP_LAYER_DIR="${SCRIPT_DIR}/k8s/app-layer"

usage() {
  cat <<'EOF'
Usage: ./deploy.sh [options]

Options:
  -n, --namespace <name>   Kubernetes namespace (default: default)
      --build              Build Docker images before deployment
      --skip-data-layer    Do not install/upgrade Kafka, Redis, Schema Registry
      --skip-app-layer     Do not deploy compute layer manifests
      --skip-locust        Do not deploy Locust manifests
      --wait-timeout <d>   Rollout timeout, e.g. 300s (default: 300s)
      --no-immutable-recovery  Disable automatic Kafka StatefulSet immutable field recovery
  -h, --help               Show this help

Examples:
  ./deploy.sh
  ./deploy.sh --namespace fraud-detection
  ./deploy.sh --skip-data-layer
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

  log "Waiting for app rollouts"
  kubectl rollout status deploy/api-gateway -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"
  kubectl rollout status deploy/enrichment-system -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"
  kubectl rollout status deploy/scoring-system -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"
  kubectl rollout status deploy/notification-system -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"

  log "App layer deployed"
}

deploy_locust() {
  log "Deploying Locust manifests"

  kubectl apply -n "$NAMESPACE" -f "${DATA_LAYER_DIR}/locust-configmap.yaml"
  kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/deployments.yaml"
  kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/services.yaml"

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

Locust Load Testing:
  kubectl port-forward svc/locust-master-ui 8089:8089 -n ${NAMESPACE}
  http://localhost:8089
EOF
}

main() {
  parse_args "$@"

  require_cmd kubectl

  log "Current context: $(kubectl config current-context)"
  kubectl get nodes >/dev/null

  ensure_namespace

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
