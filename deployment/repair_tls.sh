#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this repair with sudo." >&2
    exit 1
fi

deployment_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
was_active=no
if systemctl is-active --quiet worksheet-worker.service; then
    was_active=yes
    systemctl stop worksheet-worker.service
fi

"${deployment_dir}/generate_tls.sh"

if [[ ${was_active} == yes ]]; then
    systemctl start worksheet-worker.service
    ready=no
    for _ in {1..40}; do
        code=
        if systemctl is-active --quiet worksheet-worker.service \
            && code=$(curl --silent --output /dev/null --write-out '%{http_code}' \
                --max-time 1 --cacert /etc/messenger-worksheets/ca.crt \
                --request POST --data '{}' \
                https://127.0.0.1:8443/v1/codex-worksheet-jobs 2>/dev/null) \
            && [[ ${code} == 401 ]]; then
            ready=yes
            break
        fi
        sleep 0.25
    done
    if [[ ${ready} != yes ]]; then
        echo "Worker did not become TLS-ready within 10 seconds." >&2
        exit 1
    fi
    echo "Worker restarted; unauthenticated RPC returned HTTP 401 as expected."
else
    echo "Worker was inactive and remains inactive."
fi
