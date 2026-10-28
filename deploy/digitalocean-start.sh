#!/usr/bin/env bash
# Requires a server-only /opt/dianjing/runtime.env (chmod 600).
set -euo pipefail
cd /opt/dianjing/app
test -s /opt/dianjing/runtime.env
docker run -d --name dianjing --restart unless-stopped \
  --env-file /opt/dianjing/runtime.env \
  --publish 127.0.0.1:8080:8080 \
  --mount type=bind,source=/opt/dianjing/state,target=/data \
  --log-opt max-size=10m --log-opt max-file=3 \
  dianjing:local
