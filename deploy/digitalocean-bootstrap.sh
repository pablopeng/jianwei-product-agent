#!/usr/bin/env bash
# Run on a fresh Ubuntu Droplet as root; no secrets in this file.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y docker.io git curl
systemctl enable --now docker
if ! swapon --show | grep -q /swapfile; then
  if [ ! -e /swapfile ]; then fallocate -l 1G /swapfile; chmod 600 /swapfile; mkswap /swapfile; fi
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
install -d -m 700 /opt/dianjing
if [ ! -d /opt/dianjing/app/.git ]; then
  git clone https://github.com/pablopeng/jianwei-product-agent.git /opt/dianjing/app
fi
install -d -o 10001 -g 10001 -m 700 /opt/dianjing/state
cd /opt/dianjing/app
docker build -t dianjing:local .
printf '%s\n' 'Image ready. Configure /opt/dianjing/runtime.env and HTTPS proxy before starting.'
