#!/usr/bin/env bash
set -euo pipefail

APP_PORT="${APP_PORT:-8000}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
TUNNEL_CONTAINER="${TUNNEL_CONTAINER:-brain-sync-tunnel}"
TUNNEL_IMAGE="${TUNNEL_IMAGE:-cloudflare/cloudflared:latest}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Erro: docker nao encontrado no PATH." >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "Erro: curl nao encontrado no PATH." >&2
  exit 1
fi

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "Erro: arquivo $COMPOSE_FILE nao encontrado." >&2
  exit 1
fi

echo "[1/4] Subindo stack de producao com sudo docker compose..."
sudo docker compose -f "$COMPOSE_FILE" up -d --build

echo "[2/4] Aguardando /health em http://127.0.0.1:${APP_PORT}/health ..."
for i in {1..40}; do
  if curl -fsS "http://127.0.0.1:${APP_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
  if [[ "$i" -eq 40 ]]; then
    echo "Erro: app nao respondeu em /health dentro do tempo esperado." >&2
    exit 1
  fi
done

echo "[3/4] Reiniciando container do tunel (se existir)..."
sudo docker rm -f "$TUNNEL_CONTAINER" >/dev/null 2>&1 || true

echo "[4/4] Subindo tunel Cloudflare Quick Tunnel..."
sudo docker run -d \
  --name "$TUNNEL_CONTAINER" \
  --network host \
  "$TUNNEL_IMAGE" \
  tunnel --no-autoupdate --url "http://127.0.0.1:${APP_PORT}" >/dev/null

URL=""
for _ in {1..30}; do
  URL="$(sudo docker logs "$TUNNEL_CONTAINER" 2>&1 | grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' | tail -n 1 || true)"
  if [[ -n "$URL" ]]; then
    break
  fi
  sleep 1
done

if [[ -z "$URL" ]]; then
  echo "Tunel iniciado, mas nao foi possivel extrair a URL automaticamente." >&2
  echo "Use: sudo docker logs $TUNNEL_CONTAINER"
  exit 1
fi

echo "URL externa: $URL"
echo "Logs do tunel: sudo docker logs -f $TUNNEL_CONTAINER"
echo "Parar tunel: sudo docker rm -f $TUNNEL_CONTAINER"
echo "Parar app de producao: sudo docker compose -f $COMPOSE_FILE down"
