#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 server for Listing Agent. Run as root:
#   bash setup-server.sh
set -euo pipefail

echo "==> Updating the system"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y
apt-get install -y ca-certificates curl git ufw openssl

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker"
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

if ! swapon --show | grep -q .; then
  echo "==> Adding 2 GB swap (helps small servers during builds)"
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> Firewall: only SSH, HTTP and HTTPS"
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "==> Automatic security updates"
apt-get install -y unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades || true

echo
echo "Done. Next: clone the project and run deploy/make-env.sh (see deploy/README.md)."
