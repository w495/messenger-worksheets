#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this repair with sudo." >&2
    exit 1
fi

deployment_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
api_active=no
local_active=no
sandbox_active=no
if systemctl is-active --quiet worksheet-worker.service; then
    api_active=yes
    systemctl stop worksheet-worker.service
fi
if systemctl is-active --quiet worksheet-worker-local-user.service; then
    local_active=yes
    systemctl stop worksheet-worker-local-user.service
fi
if systemctl is-active --quiet worksheet-worker-sandbox-admin.service; then
    sandbox_active=yes
    systemctl stop worksheet-worker-sandbox-admin.service
fi

"${deployment_dir}/generate_tls.sh"

restart_and_check() {
    local service=$1 port=$2
    systemctl start "${service}"
    ready=no
    for _ in {1..40}; do
        code=
        if systemctl is-active --quiet "${service}" \
            && code=$(curl --silent --output /dev/null --write-out '%{http_code}' \
                --max-time 1 --cacert /etc/messenger-worksheets/ca.crt \
                --request POST --data '{}' \
                "https://127.0.0.1:${port}/v1/codex-worksheet-jobs" 2>/dev/null) \
            && [[ ${code} == 401 ]]; then
            ready=yes
            break
        fi
        sleep 0.25
    done
    if [[ ${ready} != yes ]]; then
        echo "${service} did not become TLS-ready within 10 seconds." >&2
        exit 1
    fi
    echo "${service} restarted; unauthenticated RPC returned HTTP 401."
}

if [[ ${api_active} == yes ]]; then
    restart_and_check worksheet-worker.service 8443
fi
if [[ ${local_active} == yes ]]; then
    restart_and_check worksheet-worker-local-user.service 8444
fi
if [[ ${sandbox_active} == yes ]]; then
    restart_and_check worksheet-worker-sandbox-admin.service 8445
fi
if [[ ${api_active} == no && ${local_active} == no && ${sandbox_active} == no ]]; then
    echo "All workers were inactive and remain inactive."
fi
