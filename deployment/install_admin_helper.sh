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
if ! id -u worksheet-chat-admin >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/messenger-worksheets-chat-admin \
        --no-create-home --shell /usr/sbin/nologin worksheet-chat-admin
fi
install -d -m 0700 -o worksheet-chat-admin -g worksheet-chat-admin \
    /var/lib/messenger-worksheets-chat-admin

deployment_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "${deployment_dir}/.." && pwd)
install -d -m 0750 -o root -g operator /etc/messenger-worksheets/local-user
install -m 0644 -o root -g operator /etc/messenger-worksheets/worker/server.crt \
    /etc/messenger-worksheets/local-user/server.crt
install -m 0640 -o root -g operator /etc/messenger-worksheets/worker/server.key \
    /etc/messenger-worksheets/local-user/server.key
install -d -m 0750 -o root -g worksheet-chat-admin /etc/messenger-worksheets/sandbox-admin
install -m 0644 -o root -g worksheet-chat-admin /etc/messenger-worksheets/worker/server.crt \
    /etc/messenger-worksheets/sandbox-admin/server.crt
install -m 0640 -o root -g worksheet-chat-admin /etc/messenger-worksheets/worker/server.key \
    /etc/messenger-worksheets/sandbox-admin/server.key
instance_config=${deployment_dir}/instances.local.json
[[ -f ${instance_config} ]] || { echo "Missing deployment/instances.local.json" >&2; exit 1; }
PYTHONPATH=/opt/messenger-worksheets/app \
    /opt/messenger-worksheets/venv/bin/python "${deployment_dir}/provision_instances.py" \
    --config "${instance_config}"

systemd-analyze verify \
    "${deployment_dir}/worksheet-worker.service" \
    "${deployment_dir}/worksheet-worker-local-user.service" \
    "${deployment_dir}/worksheet-worker-sandbox-admin.service" \
    "${deployment_dir}/worksheet-gateway-api.service" \
    "${deployment_dir}/worksheet-gateway-local-user.service" \
    "${deployment_dir}/worksheet-gateway-sandbox-admin.service"
for module in codex_rpc.py codex_worker.py personal_messenger.py; do
    install -m 0644 -o root -g root \
        "${repo_root}/messenger-worksheets/messenger_worksheets/${module}" \
        "/opt/messenger-worksheets/app/messenger_worksheets/${module}"
done
install -m 0644 -o root -g root \
    "${repo_root}/messenger-worksheets/messenger_worksheets/cloud_gateway.py" \
    /opt/messenger-worksheets/app/messenger_worksheets/cloud_gateway.py
install -m 0644 -o root -g root \
    "${repo_root}/messenger-worksheets/messenger_codex/bridge.py" \
    /opt/messenger-worksheets/app/messenger_codex/bridge.py
install -m 0644 -o root -g root "${deployment_dir}/worksheet-worker-local-user.service" \
    /etc/systemd/system/worksheet-worker-local-user.service
install -m 0644 -o root -g root "${deployment_dir}/worksheet-worker.service" \
    /etc/systemd/system/worksheet-worker.service
install -m 0644 -o root -g root "${deployment_dir}/worksheet-worker-sandbox-admin.service" \
    /etc/systemd/system/worksheet-worker-sandbox-admin.service
install -m 0644 -o root -g root "${deployment_dir}/worksheet-gateway-api.service" \
    /etc/systemd/system/worksheet-gateway-api.service
install -m 0644 -o root -g root "${deployment_dir}/worksheet-gateway-local-user.service" \
    /etc/systemd/system/worksheet-gateway-local-user.service
install -m 0644 -o root -g root "${deployment_dir}/worksheet-gateway-sandbox-admin.service" \
    /etc/systemd/system/worksheet-gateway-sandbox-admin.service
install -m 0644 -o root -g root "${deployment_dir}/worksheet-gateway.service" \
    /etc/systemd/system/worksheet-gateway.service
systemctl daemon-reload
systemctl disable --now worksheet-gateway.service

install -d -m 0755 -o root -g root /opt/messenger-worksheets/admin
for script in check_messenger.py diagnose_openai.py doctor_openai.py login_codex.py smoke_test_worker.py; do
    install -m 0755 -o root -g root "${deployment_dir}/${script}" \
        "/opt/messenger-worksheets/admin/${script}"
done
install -m 0755 -o root -g root "${deployment_dir}/worksheet-admin" \
    /usr/local/sbin/worksheet-admin

sudoers_tmp=$(mktemp)
trap 'rm -f -- "${sudoers_tmp}"' EXIT
for operation in status check-messenger-api check-messenger-local \
    diagnose-openai doctor-openai login-codex smoke-api smoke-local set-native-codex \
    login-sandbox-admin smoke-sandbox \
    set-profile-gpt-5.3-codex-high \
    start-worker-api restart-worker-api stop-worker-api \
    start-worker-local restart-worker-local stop-worker-local \
    start-worker-sandbox restart-worker-sandbox stop-worker-sandbox \
    start-gateway-api stop-gateway-api start-gateway-local stop-gateway-local \
    start-gateway-sandbox stop-gateway-sandbox activate-sandbox rollback-local \
    start-api-instance stop-api-instance start-local-instance stop-local-instance \
    enable-instances enable-sandbox-instance enable-local-instance \
    logs-worker-api logs-worker-local logs-worker-sandbox \
    logs-gateway-api logs-gateway-local logs-gateway-sandbox revoke; do
    printf 'operator ALL=(root) NOPASSWD: /usr/local/sbin/worksheet-admin %s\n' "${operation}"
done > "${sudoers_tmp}"
chmod 0440 "${sudoers_tmp}"
visudo -cf "${sudoers_tmp}"
install -m 0440 -o root -g root "${sudoers_tmp}" \
    /etc/sudoers.d/messenger-worksheets-admin

rm -f -- "${sudoers_tmp}"
trap - EXIT

echo "Temporary restricted worksheet-admin access installed for operator."
