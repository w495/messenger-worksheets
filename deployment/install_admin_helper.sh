#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi
if ! id -u operator >/dev/null 2>&1; then
    echo "Expected operator account operator is missing." >&2
    exit 1
fi

deployment_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "${deployment_dir}/.." && pwd)
install -m 0644 -o root -g root \
    "${repo_root}/messenger-worksheets/messenger_worksheets/codex_worker.py" \
    /opt/messenger-worksheets/app/messenger_worksheets/codex_worker.py
install -d -m 0755 -o root -g root /opt/messenger-worksheets/admin
for script in check_messenger.py diagnose_openai.py login_codex.py smoke_test_worker.py; do
    install -m 0755 -o root -g root "${deployment_dir}/${script}" \
        "/opt/messenger-worksheets/admin/${script}"
done
install -m 0755 -o root -g root "${deployment_dir}/worksheet-admin" \
    /usr/local/sbin/worksheet-admin

install -d -m 0750 -o root -g operator /etc/messenger-worksheets/local-user
install -m 0644 -o root -g operator /etc/messenger-worksheets/worker/server.crt \
    /etc/messenger-worksheets/local-user/server.crt
install -m 0640 -o root -g operator /etc/messenger-worksheets/worker/server.key \
    /etc/messenger-worksheets/local-user/server.key

transport_token=
while IFS='=' read -r key value; do
    if [[ ${key} == WORKSHEET_CODEX_TRANSPORT_TOKEN ]]; then
        transport_token=${value}
    fi
done < /etc/messenger-worksheets/gateway.env
if [[ ${#transport_token} -lt 32 ]]; then
    echo "Gateway transport token is missing or too short." >&2
    exit 1
fi
local_env_tmp=$(mktemp)
sudoers_tmp=
cleanup() {
    rm -f -- "${local_env_tmp}"
    [[ -z ${sudoers_tmp} ]] || rm -f -- "${sudoers_tmp}"
}
trap cleanup EXIT
{
    printf 'WORKSHEET_CODEX_PROVIDER=local_user\n'
    printf 'WORKSHEET_CODEX_BINARY=/usr/local/bin/codex\n'
    printf 'WORKSHEET_CODEX_LISTEN=127.0.0.1\n'
    printf 'WORKSHEET_CODEX_PORT=8443\n'
    printf 'WORKSHEET_CODEX_TLS_CERT=/etc/messenger-worksheets/local-user/server.crt\n'
    printf 'WORKSHEET_CODEX_TLS_KEY=/etc/messenger-worksheets/local-user/server.key\n'
    printf 'WORKSHEET_CODEX_TRANSPORT_TOKEN=%s\n' "${transport_token}"
} > "${local_env_tmp}"
install -m 0600 -o root -g root "${local_env_tmp}" \
    /etc/messenger-worksheets/local-user.env
unset transport_token
install -m 0644 -o root -g root "${deployment_dir}/worksheet-worker-local-user.service" \
    /etc/systemd/system/worksheet-worker-local-user.service
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/worksheet-worker-local-user.service

sudoers_tmp=$(mktemp)
for operation in status check-messenger diagnose-openai login-codex smoke-worker set-native-codex \
    set-profile-gpt-5.3-codex-high \
    start-worker restart-worker stop-worker switch-to-local-user stop-worker-local \
    switch-to-api-key start-gateway stop-gateway \
    logs-worker logs-worker-local logs-gateway revoke; do
    printf 'operator ALL=(root) NOPASSWD: /usr/local/sbin/worksheet-admin %s\n' "${operation}"
done > "${sudoers_tmp}"
chmod 0440 "${sudoers_tmp}"
visudo -cf "${sudoers_tmp}"
install -m 0440 -o root -g root "${sudoers_tmp}" \
    /etc/sudoers.d/messenger-worksheets-admin

cleanup
trap - EXIT

echo "Temporary restricted worksheet-admin access installed for operator."
