#!/bin/bash
set -e

# 1. Assicuriamoci che la cartella per i report esista prima che i server partano
mkdir -p /app/reports/incidents

echo "Avvio Health Server della dashboard sulla porta ${HEALTH_CHECK_PORT:-8002}..."
uvicorn src.agent_trend.health_server:app --host 0.0.0.0 --port "${HEALTH_CHECK_PORT:-8002}" &

echo "Avvio Streamlit (Dashboard UI) sulla porta 8501..."
streamlit run src/agent_trend/dashboard.py --server.port 8501 --server.address 0.0.0.0 &

# 2. Aspetta che uno dei due processi termini. Se uno crasha, il container muore (comportamento corretto in K8s).
wait -n

# Esci con lo stesso codice di errore del processo crashato
exit $?