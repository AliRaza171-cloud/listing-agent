#!/usr/bin/env bash
# Creates the production .env with fresh random secrets. Run from the repo root:
#   bash deploy/make-env.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  echo ".env already exists — not overwriting it. Edit it with: nano .env"
  exit 1
fi

read -rp "API address (e.g. listingagent.duckdns.org, no https://): " API_DOMAIN
read -rp "Website address (e.g. https://listing-agent.vercel.app): " APP_URL
read -rp "AI provider — gemini or openai [openai]: " AI_PROVIDER; AI_PROVIDER="${AI_PROVIDER:-openai}"
case "$AI_PROVIDER" in gemini|openai) ;; *) echo "Type gemini or openai"; exit 1 ;; esac
read -rsp "$AI_PROVIDER API key (hidden while typing): " AI_KEY; echo

API_DOMAIN="${API_DOMAIN#https://}"; API_DOMAIN="${API_DOMAIN%/}"
APP_URL="${APP_URL%/}"
rand() { openssl rand -base64 48 | tr -d '/+=\n' | cut -c1-"$1"; }
FERNET=$(openssl rand -base64 32 | tr '+/' '-_')

cp .env.example .env
set_kv() {  # set_kv KEY VALUE  — replaces KEY=... or appends it
  local k="$1" v="$2"
  if grep -q "^$k=" .env; then
    python3 - "$k" "$v" <<'PY'
import sys, re
k, v = sys.argv[1], sys.argv[2]
s = open(".env").read()
s = re.sub(rf"^{re.escape(k)}=.*$", lambda m: f"{k}={v}", s, count=1, flags=re.M)
open(".env", "w").write(s)
PY
  else
    echo "$k=$v" >> .env
  fi
}

set_kv POSTGRES_PASSWORD "$(rand 32)"
set_kv INTERNAL_TOKEN "$(rand 64)"
set_kv JWT_SECRET "$(rand 64)"
set_kv CREDENTIALS_ENCRYPTION_KEY "$FERNET"
set_kv ALLOW_HTTP_STORES false
set_kv API_DOMAIN "$API_DOMAIN"
set_kv PUBLIC_BASE_URL "https://$API_DOMAIN"
set_kv APP_URL "$APP_URL"
set_kv ALLOWED_ORIGINS "[\"$APP_URL\"]"
set_kv AI_PROVIDER "$AI_PROVIDER"
set_kv AI_API_KEY "$AI_KEY"
set_kv STT_PROVIDER "$AI_PROVIDER"
set_kv STT_API_KEY "$AI_KEY"
if [ "$AI_PROVIDER" = openai ]; then set_kv AI_MODEL gpt-5-mini; set_kv STT_MODEL gpt-4o-mini-transcribe; fi
chmod 600 .env

echo
echo ".env created with new random secrets."
echo "Now add your other keys:  nano .env   (STRIPE_*, SAFEPAY_*, and later SHOPIFY_*, DARAZ_*, EBAY_*)"
echo "IMPORTANT: back up the CREDENTIALS_ENCRYPTION_KEY line somewhere safe — losing it"
echo "makes every saved store connection unreadable."
