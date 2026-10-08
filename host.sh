#!/usr/bin/env bash
# Share the app for internal testing through a free Cloudflare quick tunnel.
# Keeps the Mac awake while running. Stop with Ctrl+C.
set -e
cd "$(dirname "$0")"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
if [ -z "$BOM_PASSWORD" ]; then
  echo "Set BOM_PASSWORD in .env first, so the public link is password protected." >&2
  exit 1
fi
if [ ! -x .bin/cloudflared ]; then
  mkdir -p .bin
  curl -sL -o .bin/cloudflared.tgz https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz
  tar -xzf .bin/cloudflared.tgz -C .bin && rm .bin/cloudflared.tgz && chmod +x .bin/cloudflared
fi
if ! curl -s -o /dev/null http://127.0.0.1:${PORT:-8000}/healthz; then
  ./run.sh > data/server.log 2>&1 &
  sleep 5
fi
echo "Login: username '${BOM_USERNAME:-team}', password from BOM_PASSWORD in .env"
echo "The public https://....trycloudflare.com link appears below."
exec caffeinate -dimsu .bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:${PORT:-8000}
