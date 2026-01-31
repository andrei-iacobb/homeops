#!/bin/sh
set -e

# Build API URL - add https:// if not present
API_URL="${TRUENAS_HOST}"
case "${API_URL}" in
  http*) ;;
  *) API_URL="https://${API_URL}" ;;
esac

cat > /tmp/config.yaml <<YAML
listen_port: 9814
targets:
  - name: "truenas"
    api_url: "${API_URL}"
    api_token: "${TRUENAS_API_KEY}"
    verify_ssl: false
YAML

exec python3 /app/truenas-exporter.py --config /tmp/config.yaml
