#!/bin/sh
set -eu

ALLOWLIST_PATH="${PROXY_ALLOWLIST_PATH:-/etc/rsi/proxy_allowlist.txt}"
CERT_EXPORT_DIR="${PROXY_CERT_EXPORT_DIR:-/proxy-ca-cert}"

mkdir -p "$(dirname "$ALLOWLIST_PATH")" /var/log/rsi "$CERT_EXPORT_DIR"

if [ ! -f "$ALLOWLIST_PATH" ]; then
    printf '%s\n' "${PROXY_ALLOWLIST:-}" | tr ',' '\n' | sed '/^[[:space:]]*$/d' > "$ALLOWLIST_PATH"
fi

(
    while [ ! -f /root/.mitmproxy/mitmproxy-ca-cert.pem ]; do
        sleep 1
    done
    cp /root/.mitmproxy/mitmproxy-ca-cert.pem "$CERT_EXPORT_DIR/rsi-egress.crt"
    chmod 0644 "$CERT_EXPORT_DIR/rsi-egress.crt"
) &

exec mitmdump \
    --mode regular \
    --listen-port 8084 \
    -s /opt/proxy/addon.py \
    --set allow_domains="${PROXY_ALLOWLIST:-}"
