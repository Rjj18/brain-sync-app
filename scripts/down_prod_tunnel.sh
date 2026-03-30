#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
TUNNEL_CONTAINER="${TUNNEL_CONTAINER:-brain-sync-tunnel}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Erro: docker nao encontrado no PATH." >&2
  exit 1
fi

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "Erro: arquivo $COMPOSE_FILE nao encontrado." >&2
  exit 1
fi

echo "[1/2] Encerrando tunel (se estiver ativo)..."
sudo docker rm -f "$TUNNEL_CONTAINER" >/dev/null 2>&1 || true

echo "[2/2] Encerrando stack de producao..."
sudo docker compose -f "$COMPOSE_FILE" down

echo "Concluido: tunel e containers de producao foram encerrados."
