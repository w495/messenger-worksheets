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
# Перед ротацией останавливаются только реально активные workers; их исходное
# состояние фиксируется, чтобы repair не включил ранее выключенный профиль.
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

# Запускает один ранее активный worker и не считает systemd Active достаточным:
# до 10 секунд проверяется реальный TLS handshake с новым CA и ожидаемый HTTP 401
# на запрос без transport token. Секреты и response body при этом не выводятся.
restart_and_check() {
    local service=$1 port=$2 ca_file=$3
    systemctl start "${service}"
    ready=no
    for _ in {1..40}; do
        code=
        if systemctl is-active --quiet "${service}" \
            && code=$(curl --silent --output /dev/null --write-out '%{http_code}' \
                --max-time 1 --cacert "${ca_file}" \
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

# Восстанавливаются только профили, отмеченные active до repair; если ни один не
# работал, новые сертификаты остаются на диске, но сервисы не запускаются.
if [[ ${api_active} == yes ]]; then
    restart_and_check worksheet-worker.service 8443 /etc/messenger-worksheets/ca-api.crt
fi
if [[ ${local_active} == yes ]]; then
    restart_and_check worksheet-worker-local-user.service 8444 \
        /etc/messenger-worksheets/ca-local-user.crt
fi
if [[ ${sandbox_active} == yes ]]; then
    restart_and_check worksheet-worker-sandbox-admin.service 8445 \
        /etc/messenger-worksheets/ca-sandbox-admin.crt
fi
if [[ ${api_active} == no && ${local_active} == no && ${sandbox_active} == no ]]; then
    echo "All workers were inactive and remain inactive."
fi
