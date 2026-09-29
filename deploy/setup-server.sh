#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 server (x86 or ARM, e.g. Hetzner or Oracle Cloud Free).
# Run as root:   sudo -i   then   bash deploy/setup-server.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run as root: sudo -i, then run this again."; exit 1; }

echo "==> Updating the system"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y
apt-get install -y ca-certificates curl git openssl python3

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

if [ -f /etc/iptables/rules.v4 ] && command -v netfilter-persistent >/dev/null 2>&1; then
  # Oracle Cloud's Ubuntu images ship their own iptables rules that reject everything except SSH.
  # Keep them (don't mix in ufw) and just open HTTP/HTTPS in front of the reject rule.
  echo "==> Firewall (Oracle-style iptables): opening HTTP and HTTPS"
  for port in 443 80; do
    iptables -C INPUT -p tcp -m state --state NEW --dport "$port" -j ACCEPT 2>/dev/null \
      || iptables -I INPUT 1 -p tcp -m state --state NEW --dport "$port" -j ACCEPT
  done
  netfilter-persistent save
  echo "    Also open TCP 80 and 443 in the Oracle console: VCN -> Security List -> Ingress Rules."
else
  echo "==> Firewall: only SSH, HTTP and HTTPS"
  apt-get install -y ufw
  ufw allow OpenSSH
  # Also keep any extra SSH port open (e.g. "Port 2222" when an internet provider blocks port 22),
  # so enabling the firewall never locks you out.
  for p in $(grep -hE '^\s*Port\s+[0-9]+' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null | awk '{print $2}' | sort -u); do
    ufw allow "$p/tcp"
  done
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw --force enable
fi

echo "==> Automatic security updates"
apt-get install -y unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades || true

echo
echo "Done. Next: clone the project and run deploy/make-env.sh (see deploy/README.md)."
