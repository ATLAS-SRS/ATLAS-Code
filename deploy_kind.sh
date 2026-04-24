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

CLUSTER_NAME="fraud-detection-lab"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_LAYER_DIR="${SCRIPT_DIR}/k8s/data-layer"
PLT_LAYER_DIR="${SCRIPT_DIR}/k8s/plt-layer"
APP_LAYER_DIR="${SCRIPT_DIR}/k8s/app-layer"
KIND_CONFIG="${SCRIPT_DIR}/k8s/kind-config.yaml"
INGRESS_MANIFEST_URL="https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml"
INGRESS_NAMESPACE="ingress-nginx"
INGRESS_CONTROLLER_LABEL="app.kubernetes.io/component=controller,app.kubernetes.io/name=ingress-nginx"

SCALING_AGENT_MANIFEST="${APP_LAYER_DIR}/agent-guardian.yaml"
SERVICEMONITORS_MANIFEST="${APP_LAYER_DIR}/servicemonitors.yaml"

usage() {
	cat <<'EOF'
Usage: ./deploy_kind.sh [options]

Options:
	-n, --namespace <name>   Kubernetes namespace (default: default)
			--build              Build Docker images before deployment and load them into kind
			--skip-data-layer    Do not install/upgrade Kafka, Redis, Schema Registry
			--skip-plt           Do not install/upgrade Prometheus, Loki, Grafana
			--skip-app-layer     Do not deploy compute layer manifests
			--skip-locust        Do not deploy Locust config and waits
			--wait-timeout <d>   Rollout timeout, e.g. 300s (default: 300s)
			--no-immutable-recovery  Disable automatic Kafka StatefulSet immutable field recovery
	-h, --help               Show this help

Examples:
	./deploy_kind.sh
	./deploy_kind.sh --build
	./deploy_kind.sh --skip-data-layer
	./deploy_kind.sh --skip-plt
	./deploy_kind.sh --skip-locust
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

ensure_llm_credentials_secret() {
  if [[ ! -f "$SCALING_AGENT_MANIFEST" ]]; then
    return 0
  fi

  if ! grep -q "name: llm-credentials" "$SCALING_AGENT_MANIFEST"; then
    return 0
  fi

  if kubectl get secret llm-credentials -n "$NAMESPACE" >/dev/null 2>&1; then
    log "Found existing llm-credentials secret"
    return 0
  fi

  if [[ -n "${LLM_API_KEY:-}" ]]; then
    log "Creating llm-credentials secret from LLM_API_KEY environment variable"
    kubectl create secret generic llm-credentials \
      --namespace "$NAMESPACE" \
      --from-literal=LLM_API_KEY="$LLM_API_KEY" \
      --dry-run=client -o yaml | kubectl apply -f -
    return 0
  fi

  echo "ERROR: secret llm-credentials not found in namespace ${NAMESPACE} and LLM_API_KEY is not set." >&2
  echo "Set it with: export LLM_API_KEY=<your-litellm-api-key>" >&2
  echo "Or create it manually: kubectl create secret generic llm-credentials -n ${NAMESPACE} --from-literal=LLM_API_KEY=<your-litellm-api-key>" >&2
  exit 1
}

ensure_cluster() {
	if ! kind get clusters | grep -Fxq "$CLUSTER_NAME"; then
		if [[ ! -f "$KIND_CONFIG" ]]; then
			echo "ERROR: kind config not found at ${KIND_CONFIG}" >&2
			exit 1
		fi

		log "Creating kind cluster ${CLUSTER_NAME}"
		kind create cluster --name "$CLUSTER_NAME" --config "$KIND_CONFIG"
	else
		log "Kind cluster ${CLUSTER_NAME} already exists"
	fi

	kind export kubeconfig --name "$CLUSTER_NAME"
	kubectl config use-context "kind-${CLUSTER_NAME}" >/dev/null
	log "Current context: $(kubectl config current-context)"
	kubectl wait --for=condition=Ready node --all --timeout="$WAIT_TIMEOUT"
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

build_and_load_image() {
	local image_name="$1"
	local dockerfile_path="$2"
	local context_path="$3"

	log "Building ${image_name}"
	docker build -t "$image_name" -f "$dockerfile_path" "$context_path"

	log "Loading ${image_name} into kind cluster ${CLUSTER_NAME}"
	kind load docker-image "$image_name" --name "$CLUSTER_NAME"
}

build_images() {
	log "Building Docker images for kind"

	build_and_load_image "atlas/api-gateway:latest" "${SCRIPT_DIR}/gateway/Dockerfile" "${SCRIPT_DIR}/gateway"
	build_and_load_image "atlas/enrichment-system:latest" "${SCRIPT_DIR}/enrichment-system/Dockerfile" "${SCRIPT_DIR}/enrichment-system"
	build_and_load_image "atlas/scoring-system:fix-readiness-2" "${SCRIPT_DIR}/scoring-system/Dockerfile" "${SCRIPT_DIR}/scoring-system"
	build_and_load_image "atlas/notification-system:latest" "${SCRIPT_DIR}/notification-system/Dockerfile" "${SCRIPT_DIR}/notification-system"
	# build_and_load_image "atlas/agent-guardian:latest" "${SCRIPT_DIR}/agent-guardian/Dockerfile" "${SCRIPT_DIR}/agent-guardian"
	build_and_load_image "atlas/kafka-connect:latest" "${SCRIPT_DIR}/Dockerfile.connect" "${SCRIPT_DIR}"

	log "Docker images built and loaded successfully"
}

deploy_ingress_controller() {
	log "Installing NGINX Ingress Controller for kind"
	kubectl apply -f "$INGRESS_MANIFEST_URL"
	kubectl rollout status deployment/ingress-nginx-controller -n "$INGRESS_NAMESPACE" --timeout="$WAIT_TIMEOUT"
	kubectl wait --for=condition=Ready pod -n "$INGRESS_NAMESPACE" -l "$INGRESS_CONTROLLER_LABEL" --timeout="$WAIT_TIMEOUT"
	log "NGINX Ingress Controller is ready"
}

deploy_data_layer() {
	log "Deploying data layer (Kafka, Redis, Schema Registry, Kafka Connect)"

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
			kubectl delete statefulset atlas-kafka-controller atlas-kafka-broker -n "$NAMESPACE" --ignore-not-found

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

	kubectl apply -n "$NAMESPACE" -f "${DATA_LAYER_DIR}/postgres-init-configmap.yaml"

	helm upgrade --install atlas-postgres bitnami/postgresql \
		--namespace "$NAMESPACE" \
		--create-namespace \
		--set "auth.postgresPassword=${POSTGRES_PASSWORD}" \
		-f "${DATA_LAYER_DIR}/postgres-values.yaml"

	kubectl apply -n "$NAMESPACE" -f "${DATA_LAYER_DIR}/schema-registry.yaml"
	kubectl apply -n "$NAMESPACE" -f "${DATA_LAYER_DIR}/kafka-connect.yaml"

	log "Waiting for data layer rollouts"
	rollout_or_debug statefulset atlas-kafka-controller "app.kubernetes.io/name=kafka"
	rollout_or_debug statefulset atlas-kafka-broker "app.kubernetes.io/name=kafka"
	rollout_or_debug statefulset atlas-redis-master "app.kubernetes.io/name=redis"
	rollout_or_debug statefulset atlas-postgres-postgresql "app.kubernetes.io/name=postgresql"
	rollout_or_debug deployment schema-registry "app=schema-registry"
	rollout_or_debug deployment kafka-connect "app=kafka-connect"

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

deploy_plt() {
	log "Deploying PLT stack (Prometheus, Loki, Grafana)"

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
	rollout_or_debug deployment atlas-monitoring-grafana "app.kubernetes.io/name=grafana"

	log "Generating Grafana Service Account Token for MCP server..."

	# 1. Port-forward temporaneo in background
    # Usiamo & per non bloccare lo script e catturiamo il PID per chiuderlo dopo
    kubectl port-forward -n "$NAMESPACE" svc/atlas-monitoring-grafana 3000:80 >/dev/null 2>&1 &
    local PF_PID=$!
    
    # Attendiamo che il port-forward sia attivo
    sleep 5

	local GRAFANA_API_URL="http://admin:${GRAFANA_ADMIN_PASSWORD}@localhost:3000/api"

	# 2. Creazione Service Account (se non esiste già)
    # Cerchiamo se esiste già un SA chiamato 'mcp-server'
    local SA_ID=$(curl -s "$GRAFANA_API_URL/serviceaccounts/search?query=mcp-server" | jq -r '.serviceAccounts[0].id // empty')

    if [[ -z "$SA_ID" ]]; then
        log "Creating new Service Account 'mcp-server'"
        SA_ID=$(curl -s -X POST "$GRAFANA_API_URL/serviceaccounts" \
            -H "Content-Type: application/json" \
            -d '{"name":"mcp-server", "role": "Admin"}' | jq -r '.id')
    fi

    # 3. Generazione di un nuovo Token
    # Nota: I token di Grafana non sono recuperabili dopo la creazione, 
    # quindi ne creiamo uno nuovo e aggiorniamo il secret.
    log "Generating new API Token..."
    local NEW_TOKEN=$(curl -s -X POST "$GRAFANA_API_URL/serviceaccounts/$SA_ID/tokens" \
        -H "Content-Type: application/json" \
        -d '{"name":"mcp-token-'"$(date +%s)"'"}' | jq -r '.key')

    # 4. Aggiornamento Secret Kubernetes
    kubectl create secret generic grafana-mcp-credentials \
        --namespace "$NAMESPACE" \
        --from-literal=token="$NEW_TOKEN" \
        --dry-run=client -o yaml | kubectl apply -f -

    # Chiudiamo il port-forward
    kill $PF_PID
    log "Grafana MCP credentials secret updated."

	log "Deploying Grafana MCP Server..."

    kubectl apply -n "$NAMESPACE" -f "${PLT_LAYER_DIR}/grafana-mcp.yaml"
    
    # Attendi che il server MCP sia pronto
    rollout_or_debug deployment grafana-mcp-server "app.kubernetes.io/name=grafana-mcp-server"
    # --- FINE AUTOMAZIONE TOKEN MCP ---

	log "Deploying metrics-server for HPA support"
	kubectl apply -f "https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml"
	if ! kubectl get deployment metrics-server -n kube-system -o jsonpath='{.spec.template.spec.containers[0].args}' | grep -q -- '--kubelet-insecure-tls'; then
		kubectl -n kube-system patch deployment metrics-server --type=json \
			-p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' || true
	fi
	kubectl rollout status deployment/metrics-server -n kube-system --timeout="$WAIT_TIMEOUT"

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

	rollout_or_debug statefulset "$prom_sts" "app.kubernetes.io/name=prometheus"

	log "PLT stack deployed"
}

deploy_app_layer() {
	log "Deploying app layer manifests"

	kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/config.yaml"
	kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/deployments.yaml"
	kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/services.yaml"
	kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/ingress.yaml"
	kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/hpa.yaml"

	if [[ -f "$SERVICEMONITORS_MANIFEST" ]]; then
		kubectl apply -n "$NAMESPACE" -f "$SERVICEMONITORS_MANIFEST"
	fi

	log "Waiting for app rollouts"
	rollout_or_debug deployment api-gateway "app.kubernetes.io/name=api-gateway"
	rollout_or_debug deployment enrichment-system "app.kubernetes.io/name=enrichment-system"
	rollout_or_debug deployment scoring-system "app.kubernetes.io/name=scoring-system"
	rollout_or_debug deployment notification-system "app.kubernetes.io/name=notification-system"

	if [[ -f "$SCALING_AGENT_MANIFEST" ]]; then
		ensure_llm_credentials_secret
		log "Deploying agent-guardian"
		kubectl apply -n "$NAMESPACE" -f "$SCALING_AGENT_MANIFEST"
		rollout_or_debug deployment atlas-aiops-agent "app.kubernetes.io/name=atlas-aiops-agent"
	else
		log "Scaling agent manifest not found at ${SCALING_AGENT_MANIFEST}, skipping"
	fi

	if [[ "$SKIP_LOCUST" == true ]]; then
		log "Locust skipped: scaling locust deployments to zero replicas"
		kubectl scale deployment locust-master --replicas=0 -n "$NAMESPACE" >/dev/null 2>&1 || true
		kubectl scale deployment locust-worker --replicas=0 -n "$NAMESPACE" >/dev/null 2>&1 || true
	fi

	log "App layer deployed"
}

deploy_locust() {
	log "Deploying Locust config and waiting for workers"

	kubectl apply -n "$NAMESPACE" -f "${APP_LAYER_DIR}/locust-configmap.yaml"

	log "Waiting for Locust rollouts"
	rollout_or_debug deployment locust-master "app.kubernetes.io/name=locust-master"
	rollout_or_debug deployment locust-worker "app.kubernetes.io/name=locust-worker"

	log "Locust deployed"
}

print_summary() {
	log "Deployment summary"
	kubectl get pods -n "$NAMESPACE" -o wide || true
	echo
	kubectl get svc -n "$NAMESPACE" || true
	echo
	kubectl get ingress -n "$NAMESPACE" || true

	cat <<EOF

Endpoints (.nip.io):
	API Gateway:   http://fraud-api.127.0.0.1.nip.io
	Locust UI:     http://locust.127.0.0.1.nip.io
	Grafana UI:    http://grafana.127.0.0.1.nip.io
	Prometheus:    http://prometheus.127.0.0.1.nip.io

Grafana credentials:
	User: admin
	Password: ${GRAFANA_ADMIN_PASSWORD}

PostgreSQL credentials:
	User: postgres
	Password: ${POSTGRES_PASSWORD}
	Database: atlas_db

EOF
}

main() {
	parse_args "$@"

	if [[ -z "${GRAFANA_ADMIN_PASSWORD:-}" ]]; then
		echo "ERROR: GRAFANA_ADMIN_PASSWORD non impostata o vuota. Esporta la variabile prima di eseguire ./deploy_kind.sh." >&2
		exit 1
	fi

	if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then
		echo "ERROR: POSTGRES_PASSWORD non impostata o vuota. Esporta la variabile prima di eseguire ./deploy_kind.sh." >&2
		exit 1
	fi

	require_cmd kubectl
	require_cmd kind
	require_cmd docker

	if [[ "$SKIP_DATA_LAYER" == false ]] || [[ "$SKIP_PLT" == false ]]; then
		require_cmd helm
	fi

	ensure_cluster
	ensure_namespace
	deploy_ingress_controller

	if [[ "$SKIP_APP_LAYER" == true ]] && [[ "$SKIP_LOCUST" == false ]]; then
		log "Locust depends on the app-layer manifests, so it will be skipped as well"
		SKIP_LOCUST=true
	fi

	if [[ "$NAMESPACE" != "default" ]]; then
		log "WARNING: app-layer ConfigMap and connector job still reference *.default.svc.cluster.local endpoints."
		log "If deploying outside 'default', update k8s/app-layer/config.yaml and related manifests accordingly."
	fi

	if [[ "$BUILD_IMAGES" == true ]]; then
		build_images
	fi

	if [[ "$SKIP_PLT" == false ]]; then
		deploy_plt
	else
		log "Skipping PLT deployment"
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
