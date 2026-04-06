#!/usr/bin/env bash
set -e
docker compose up -d --build

# Sanity check
sleep 2
if curl -sf http://localhost:5000/ > /dev/null; then
  echo "App running at http://localhost:5000"
  NGROK_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null | python3 -c "import sys,json; t=json.load(sys.stdin)['tunnels'][0]; print(t['public_url'])" 2>/dev/null || true)
  if [[ -n "$NGROK_URL" ]]; then
    echo "Public URL:  $NGROK_URL"
  fi
else
  echo "ERROR: App did not start. Check: docker logs prv-app-1"
  exit 1
fi
