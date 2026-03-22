#!/bin/bash
set -e

for _ in $(seq 1 30); do
    if [ -f /usr/local/share/ca-certificates/rsi-egress.crt ]; then
        update-ca-certificates >/dev/null 2>&1
        echo "[entrypoint] CA cert installed" >&2
        break
    fi
    sleep 1
done

if [ ! -f /usr/local/share/ca-certificates/rsi-egress.crt ]; then
    echo "[entrypoint] WARNING: no proxy CA cert found" >&2
fi

git config --global user.email "agent@rsi-sandbox"
git config --global user.name "rsi-agent"

# Optional: set up git remote for pushing session state
if [ -n "${GIT_REMOTE_URL:-}" ]; then
    cd /workspace/agent 2>/dev/null || true
    if ! git remote get-url origin >/dev/null 2>&1; then
        git remote add origin "$GIT_REMOTE_URL"
        echo "[entrypoint] git remote added: $GIT_REMOTE_URL" >&2
    fi
    cd - >/dev/null 2>/dev/null || true
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

exec python /opt/supervisor/supervisor.py
