#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi
if ! id -u w495 >/dev/null 2>&1; then
    echo "Expected operator account w495 is missing." >&2
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
# Каждый worker-профиль должен иметь собственную CA/leaf/key цепочку. При первом
# обновлении со старой общей цепочки генератор создаёт все три набора; ключи
# никогда не копируются между worksheet-worker, w495 и worksheet-chat-admin.
if [[ ! -s /etc/messenger-worksheets/ca-api.crt \
      || ! -s /etc/messenger-worksheets/ca-local-user.crt \
      || ! -s /etc/messenger-worksheets/ca-sandbox-admin.crt \
      || ! -s /etc/messenger-worksheets/worker/server.key \
      || ! -s /etc/messenger-worksheets/local-user/server.key \
      || ! -s /etc/messenger-worksheets/sandbox-admin/server.key ]]; then
    "${deployment_dir}/generate_tls.sh"
fi
instance_config=${deployment_dir}/instances.local.json
[[ -f ${instance_config} ]] || { echo "Missing deployment/instances.local.json" >&2; exit 1; }
# Clean-room package и pinned requirements устанавливаются до provisioning,
# чтобы read-only identity/invite probe использовал фактически deployed backend.
install -d -m 0755 -o root -g root \
    /opt/messenger-worksheets/app/messenger_user_client
install -m 0644 -o root -g root \
    "${repo_root}/messenger-worksheets/messenger_user_client/__init__.py" \
    "${repo_root}/messenger-worksheets/messenger_user_client/client.py" \
    /opt/messenger-worksheets/app/messenger_user_client/
install -m 0644 -o root -g root \
    "${repo_root}/messenger-worksheets/requirements.txt" \
    "${repo_root}/messenger-worksheets/requirements.lock" \
    /opt/messenger-worksheets/app/
/opt/messenger-worksheets/venv/bin/python -m pip install \
    --disable-pip-version-check --require-hashes \
    --requirement /opt/messenger-worksheets/app/requirements.lock
PYTHONPATH=/opt/messenger-worksheets/app \
    /opt/messenger-worksheets/venv/bin/python "${deployment_dir}/provision_instances.py" \
    --config "${instance_config}"

systemd-analyze verify \
    "${deployment_dir}/worksheet-worker.service" \
    "${deployment_dir}/worksheet-worker-local-user.service" \
    "${deployment_dir}/worksheet-worker-sandbox-admin.service" \
    "${deployment_dir}/worksheet-gateway-api.service" \
    "${deployment_dir}/worksheet-gateway-local-user.service" \
    "${deployment_dir}/worksheet-gateway-sandbox-admin.service" \
    "${deployment_dir}/worksheet-gateway-bot.service"
# Production modules и units копируются root-owned, затем legacy aggregate
# gateway выключается: одному chat никогда не должны владеть два polling writer.
for module in bot_api.py bot_gateway.py codex_rpc.py codex_worker.py model_http.py model_catalog.py personal_messenger.py worksheet.py thread_context.py chat_commands.py request_options.py process_supervisor.py; do
    install -m 0644 -o root -g root \
        "${repo_root}/messenger-worksheets/messenger_worksheets/${module}" \
        "/opt/messenger-worksheets/app/messenger_worksheets/${module}"
done
if [[ -f ${repo_root}/messenger-client/client.py \
      && -f ${repo_root}/messenger-client/transport.py ]]; then
    install -d -m 0750 -o root -g worksheet-gateway /opt/messenger-worksheets/messenger-client
    for module in client.py transport.py; do
        install -m 0640 -o root -g worksheet-gateway \
            "${repo_root}/messenger-client/${module}" \
            "/opt/messenger-worksheets/messenger-client/${module}"
    done
fi
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
install -m 0644 -o root -g root "${deployment_dir}/worksheet-gateway-bot.service" \
    /etc/systemd/system/worksheet-gateway-bot.service
install -m 0644 -o root -g root "${deployment_dir}/worksheet-gateway.service" \
    /etc/systemd/system/worksheet-gateway.service
systemctl daemon-reload
systemctl disable --now worksheet-gateway.service

install -d -m 0755 -o root -g root /opt/messenger-worksheets/admin
for script in check_bot.py check_messenger.py diagnose_openai.py doctor_openai.py login_codex.py smoke_test_worker.py smoke_test_commands.py; do
    install -m 0755 -o root -g root "${deployment_dir}/${script}" \
        "/opt/messenger-worksheets/admin/${script}"
done
if [[ -f ${repo_root}/assets/bot-avatar.png ]]; then
    install -d -m 0755 -o root -g root /opt/messenger-worksheets/assets
    install -m 0644 -o root -g root "${repo_root}/assets/bot-avatar.png" \
        /opt/messenger-worksheets/assets/bot-avatar.png
fi
install -m 0755 -o root -g root "${deployment_dir}/worksheet-admin" \
    /usr/local/sbin/worksheet-admin

# Sudoers содержит только точные операции worksheet-admin без аргументов.
# Временный файл валидируется visudo до атомарной установки с mode 0440.
sudoers_tmp=$(mktemp)
trap 'rm -f -- "${sudoers_tmp}"' EXIT
for operation in status check-messenger-api check-messenger-local check-bot \
    check-messenger-api-cleanroom check-messenger-local-cleanroom \
    check-tls-isolation \
    diagnose-openai doctor-openai login-codex smoke-api smoke-local set-native-codex \
    login-sandbox-admin smoke-sandbox smoke-sandbox-task smoke-sandbox-link smoke-controls smoke-models \
    set-profile-gpt-5.3-codex-high \
    start-worker-api restart-worker-api stop-worker-api \
    start-worker-local restart-worker-local stop-worker-local \
    start-worker-sandbox restart-worker-sandbox stop-worker-sandbox \
    start-gateway-api stop-gateway-api start-gateway-local stop-gateway-local \
    start-gateway-sandbox stop-gateway-sandbox \
    activate-cleanroom-api rollback-legacy-api \
    activate-cleanroom-sandbox rollback-legacy-sandbox \
    start-gateway-bot restart-gateway-bot stop-gateway-bot \
    activate-sandbox rollback-local activate-bot rollback-userbot \
    start-api-instance stop-api-instance start-local-instance stop-local-instance \
    enable-instances enable-sandbox-instance enable-local-instance enable-bot-instance \
    logs-worker-api logs-worker-local logs-worker-sandbox \
    logs-gateway-api logs-gateway-local logs-gateway-sandbox logs-gateway-bot revoke; do
    printf 'w495 ALL=(root) NOPASSWD: /usr/local/sbin/worksheet-admin %s\n' "${operation}"
done > "${sudoers_tmp}"
chmod 0440 "${sudoers_tmp}"
visudo -cf "${sudoers_tmp}"
install -m 0440 -o root -g root "${sudoers_tmp}" \
    /etc/sudoers.d/messenger-worksheets-admin

rm -f -- "${sudoers_tmp}"
trap - EXIT

echo "Temporary restricted worksheet-admin access installed for w495."
